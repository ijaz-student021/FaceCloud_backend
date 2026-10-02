import hashlib
import logging

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from .models import ModeratorReport
from .renderers import render_moderator_report
from .services import (
    build_moderator_report_payload,
    expire_due_moderator_reports,
    flag_overdue_reviews,
    moderator_report_expiration_time,
)

TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)

logger = logging.getLogger(__name__)


def process_moderator_report(report_id, task_id=""):
    with transaction.atomic():
        report = ModeratorReport.objects.select_for_update().filter(pk=report_id).first()
        if report is None:
            return None
        if report.status == ModeratorReport.Status.READY:
            return report
        if report.task_id and task_id and report.task_id != task_id:
            return report
        report.status = ModeratorReport.Status.GENERATING
        report.failure_reason = ""
        report.save(update_fields=["status", "failure_reason", "updated_at"])

    try:
        report = ModeratorReport.objects.select_related("organization").get(pk=report_id)
        payload = build_moderator_report_payload(report)
        content, content_type, extension = render_moderator_report(payload, report.format)
        checksum = hashlib.sha256(content).hexdigest()
        filename = f"facecloud-{report.report_type}-report.{extension}"
        with transaction.atomic():
            locked = ModeratorReport.objects.select_for_update().filter(pk=report_id).first()
            if locked is None:
                return None
            if locked.task_id and task_id and locked.task_id != task_id:
                return locked
            if locked.report_file:
                locked.report_file.delete(save=False)
            locked.report_file.save(filename, ContentFile(content), save=False)
            locked.payload = payload
            locked.content_type = content_type
            locked.file_size = len(content)
            locked.file_checksum_sha256 = checksum
            locked.status = ModeratorReport.Status.READY
            locked.failure_reason = ""
            locked.generated_at = timezone.now()
            locked.expires_at = moderator_report_expiration_time()
            locked.save()
            return locked
    except Exception:
        logger.exception("Could not generate moderator report_id=%s.", report_id)
        with transaction.atomic():
            failed = ModeratorReport.objects.select_for_update().filter(pk=report_id).first()
            if failed is None:
                return None
            if failed.report_file:
                failed.report_file.delete(save=False)
            failed.report_file = ""
            failed.status = ModeratorReport.Status.FAILED
            failed.failure_reason = "The moderator report could not be generated safely."
            failed.file_size = 0
            failed.file_checksum_sha256 = ""
            failed.content_type = ""
            failed.expires_at = None
            failed.save()
            return failed


@shared_task(
    bind=True,
    name="moderation.generate_report",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def generate_moderator_report_task(self, report_id):
    report = ModeratorReport.objects.filter(pk=report_id).first()
    if report is None:
        return {"report_id": report_id, "status": "deleted"}
    if report.task_id and self.request.id and report.task_id != self.request.id:
        return {"report_id": report_id, "status": "superseded"}
    report = process_moderator_report(report_id, self.request.id or "")
    return {
        "report_id": report_id,
        "status": report.status if report is not None else "deleted",
    }


@shared_task(
    name="moderation.cleanup_reports",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_moderator_reports_task():
    return {"expired_reports": expire_due_moderator_reports()}


@shared_task(
    name="moderation.flag_sla_breaches",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def flag_sla_breaches_task():
    """Notify on reviews that have passed their deadline. Safe to run repeatedly."""
    return {"flagged_reviews": flag_overdue_reviews()}

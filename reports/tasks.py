import hashlib
import logging
from pathlib import Path

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from .models import ProcessingReport
from .renderers import render_report
from .services import build_report_payload

# Transient infrastructure failures only (dropped DB connections). Business
# and rendering errors are handled per-record and must never retry.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)

logger = logging.getLogger(__name__)


def process_processing_report(report_id, task_id=""):
    with transaction.atomic():
        report = ProcessingReport.objects.select_for_update().filter(pk=report_id).first()
        if report is None:
            return None
        if report.status == ProcessingReport.Status.READY:
            return report
        if report.task_id and task_id and report.task_id != task_id:
            return report
        report.status = ProcessingReport.Status.GENERATING
        report.failure_reason = ""
        report.generated_at = None
        report.save(
            update_fields=["status", "failure_reason", "generated_at", "updated_at"]
        )

    try:
        report = ProcessingReport.objects.select_related("image", "batch").get(pk=report_id)
        payload = build_report_payload(report)
        content, content_type, extension = render_report(payload, report.format)
        checksum = hashlib.sha256(content).hexdigest()
        source_name = (
            report.image.original_name if report.image_id else
            report.batch.display_name or report.batch.original_archive_name or f"batch-{report.batch_id}"
        )
        stem = Path(source_name).stem[:80] or report.report_type
        filename = f"facecloud-{stem}-report.{extension}"
        with transaction.atomic():
            locked = ProcessingReport.objects.select_for_update().get(pk=report_id)
            if locked.task_id and task_id and locked.task_id != task_id:
                return locked
            if locked.report_file:
                locked.report_file.delete(save=False)
            locked.report_file.save(filename, ContentFile(content), save=False)
            locked.payload = payload
            locked.content_type = content_type
            locked.file_size = len(content)
            locked.file_checksum_sha256 = checksum
            locked.status = ProcessingReport.Status.READY
            locked.failure_reason = ""
            locked.generated_at = timezone.now()
            locked.save()
            return locked
    except Exception:
        logger.exception("Could not generate processing report_id=%s.", report_id)
        with transaction.atomic():
            failed = ProcessingReport.objects.select_for_update().filter(pk=report_id).first()
            if failed is None:
                return None
            if failed.report_file:
                failed.report_file.delete(save=False)
            failed.status = ProcessingReport.Status.FAILED
            failed.failure_reason = "The processing report could not be generated safely."
            failed.generated_at = timezone.now()
            failed.file_size = 0
            failed.file_checksum_sha256 = ""
            failed.content_type = ""
            failed.save()
            return failed


@shared_task(
    bind=True,
    name="reports.generate",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def generate_processing_report_task(self, report_id):
    report = ProcessingReport.objects.filter(pk=report_id).first()
    if report is None:
        return {"report_id": report_id, "status": "deleted"}
    if report.task_id and self.request.id and report.task_id != self.request.id:
        return {"report_id": report_id, "status": "superseded"}
    report = process_processing_report(report_id, self.request.id or "")
    if report is not None:
        from notifications.events import notify_report_result
        notify_report_result(report)
    return {
        "report_id": report_id,
        "status": report.status if report is not None else "deleted",
    }

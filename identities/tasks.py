import logging

from celery import shared_task
from django.core.files import File
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from .export_services import (expire_due_identity_exports,
                              identity_export_expiration_time)
from .exports import build_identity_export_archive, identity_export_display_name
from .models import IdentityAnalysis, IdentityGroupExport
from .services import process_identity_analysis


logger = logging.getLogger(__name__)

# Transient infrastructure failures only (dropped DB connections). Business
# and validation errors are handled per-record and must never retry.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    bind=True,
    name="identities.analyze_image",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def analyze_image_identity_task(self, analysis_id):
    analysis = IdentityAnalysis.objects.select_related("image__batch").get(pk=analysis_id)
    if analysis.task_id and self.request.id and analysis.task_id != self.request.id:
        return {"analysis_id": analysis_id, "status": "superseded"}
    analysis = process_identity_analysis(analysis_id)
    return {"analysis_id": analysis_id, "status": analysis.status, "face_count": analysis.face_count, "assigned_count": analysis.assigned_count, "review_count": analysis.review_count}


def process_identity_group_export(export_id, task_id=""):
    with transaction.atomic():
        export = IdentityGroupExport.objects.select_for_update().filter(pk=export_id).first()
        if export is None:
            return None
        if export.status == IdentityGroupExport.Status.READY:
            return export
        if export.task_id and task_id and export.task_id != task_id:
            return export
        export.status = IdentityGroupExport.Status.GENERATING
        export.progress_percentage = 10
        export.failure_reason = ""
        export.save(update_fields=["status", "progress_percentage", "failure_reason", "updated_at"])

    temporary_path = None
    try:
        export = IdentityGroupExport.objects.select_related("identity_group").get(pk=export_id)
        temporary_path, manifest, file_size, checksum = build_identity_export_archive(export)
        display_name = identity_export_display_name(export)
        with transaction.atomic():
            locked = IdentityGroupExport.objects.select_for_update().filter(pk=export_id).first()
            if locked is None:
                return None
            if locked.task_id and task_id and locked.task_id != task_id:
                return locked
            if locked.export_file:
                locked.export_file.delete(save=False)
            with temporary_path.open("rb") as stream:
                locked.export_file.save(display_name, File(stream), save=False)
            locked.display_name = display_name
            locked.manifest = manifest
            locked.file_size = file_size
            locked.file_checksum_sha256 = checksum
            locked.status = IdentityGroupExport.Status.READY
            locked.progress_percentage = 100
            locked.failure_reason = ""
            locked.generated_at = timezone.now()
            locked.expires_at = identity_export_expiration_time()
            locked.save()
            return locked
    except Exception:
        logger.exception("Could not generate identity export_id=%s.", export_id)
        with transaction.atomic():
            failed = IdentityGroupExport.objects.select_for_update().filter(pk=export_id).first()
            if failed is None:
                return None
            if failed.export_file:
                failed.export_file.delete(save=False)
            failed.export_file = ""
            failed.status = IdentityGroupExport.Status.FAILED
            failed.progress_percentage = 100
            failed.failure_reason = "The identity-group export could not be generated safely."
            failed.file_size = 0
            failed.file_checksum_sha256 = ""
            failed.expires_at = None
            failed.save()
            return failed
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@shared_task(
    bind=True,
    name="identities.generate_group_export",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def generate_identity_group_export_task(self, export_id):
    export = IdentityGroupExport.objects.filter(pk=export_id).first()
    if export is None:
        return {"export_id": export_id, "status": "deleted"}
    if export.task_id and self.request.id and export.task_id != self.request.id:
        return {"export_id": export_id, "status": "superseded"}
    export = process_identity_group_export(export_id, self.request.id or "")
    if export is not None:
        from notifications.events import notify_identity_export_result
        notify_identity_export_result(export)
    return {"export_id": export_id, "status": export.status if export else "deleted"}


@shared_task(
    name="identities.cleanup_expired_exports",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_expired_identity_exports():
    return {"expired_exports": expire_due_identity_exports()}

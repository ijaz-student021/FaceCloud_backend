from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from .cleanup import cleanup_temporary_files
from .models import OrganizationExport
from .services import expire_due_organization_exports, process_organization_export, purge_expired_facial_data

TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    name="privacy.purge_expired_facial_data",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_facial_data_task():
    return purge_expired_facial_data()


@shared_task(
    name="privacy.cleanup_temporary_files",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_temporary_files_task():
    summary = cleanup_temporary_files()
    summary["orphaned_media"].pop("names", None)
    return summary


@shared_task(
    bind=True,
    name="privacy.generate_organization_export",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def generate_organization_export_task(self, export_id):
    export = OrganizationExport.objects.filter(pk=export_id).first()
    if export is None:
        return {"export_id": export_id, "status": "deleted"}
    if export.task_id and self.request.id and export.task_id != self.request.id:
        return {"export_id": export_id, "status": "superseded"}
    export = process_organization_export(export_id, self.request.id or "")
    return {
        "export_id": export_id,
        "status": export.status if export is not None else "deleted",
    }


@shared_task(
    name="privacy.cleanup_organization_exports",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_organization_exports_task():
    return {"expired_exports": expire_due_organization_exports()}

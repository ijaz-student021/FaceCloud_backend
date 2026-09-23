from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from .cleanup import cleanup_temporary_files
from .services import purge_expired_facial_data

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

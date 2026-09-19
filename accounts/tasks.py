from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from .lockout import purge_stale_attempts

TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    name="accounts.cleanup_login_attempts",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_login_attempts():
    return purge_stale_attempts()

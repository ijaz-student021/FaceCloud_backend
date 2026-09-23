from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from .jobs import reap_stuck_jobs

TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    name="operations.reap_stuck_jobs",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def reap_stuck_jobs_task():
    return reap_stuck_jobs()

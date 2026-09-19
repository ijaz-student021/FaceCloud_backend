from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

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

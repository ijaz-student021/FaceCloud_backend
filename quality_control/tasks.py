import logging

from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from .models import ImageQualityDecision
from .services import process_quality_decision

logger = logging.getLogger(__name__)

# Transient infrastructure failures only (dropped DB connections). Business
# and validation errors are handled per-image and must never retry.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    bind=True,
    name="quality.analyze_image",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def analyze_image_quality_task(self, decision_id):
    result = ImageQualityDecision.objects.select_related("image__batch").get(pk=decision_id)
    if result.image.batch.cancellation_requested:
        return {"decision_id": decision_id, "status": "cancelled", "decision": ""}
    if result.task_id and self.request.id and result.task_id != self.request.id:
        return {"decision_id": decision_id, "status": "superseded"}
    try:
        result = process_quality_decision(decision_id)
    except Exception:
        result.refresh_from_db()
    from notifications.events import notify_quality_result
    from uploads.progress import sync_image_progress
    notify_quality_result(result)
    from activity.services import record_activity_event
    record_activity_event(
        result.owner, "processing", f"quality_{result.decision or result.status}",
        object_type="ImageUpload", object_id=result.image_id,
    )
    sync_image_progress(result.image_id)
    _announce_suggested_merges(result)
    return {"decision_id": decision_id, "status": result.status, "decision": result.decision}


def _announce_suggested_merges(result):
    """A photograph whose verdict means it is not grouped can be the last thing its batch was waiting for: then the owner is told about folders that may match."""
    from django.conf import settings

    if not settings.IDENTITY_AUTO_ANALYZE:
        return
    try:
        from identities.suggestions import announce_suggested_merges

        announce_suggested_merges(result.image.batch_id)
    except Exception:
        logger.exception("Could not work out the suggested folder merges of image_id=%s.", result.image_id)

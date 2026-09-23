from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from .models import DuplicateAnalysis
from .services import process_duplicate_analysis

# Transient infrastructure failures only (dropped DB connections). Business
# and validation errors are handled per-image and must never retry.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    bind=True,
    name="duplicates.analyze_image",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def analyze_image_duplicates_task(self, analysis_id):
    analysis = DuplicateAnalysis.objects.select_related("image__batch").get(pk=analysis_id)
    if analysis.image.batch.cancellation_requested:
        analysis.status = DuplicateAnalysis.Status.CANCELLED
        analysis.save(update_fields=["status", "updated_at"])
        return {"analysis_id": analysis_id, "status": "cancelled", "decision": ""}
    if analysis.task_id and self.request.id and analysis.task_id != self.request.id:
        return {"analysis_id": analysis_id, "status": "superseded", "decision": ""}
    analysis = process_duplicate_analysis(analysis_id)
    return {
        "analysis_id": analysis_id,
        "status": analysis.status,
        "decision": analysis.decision,
        "match_count": analysis.match_count,
    }

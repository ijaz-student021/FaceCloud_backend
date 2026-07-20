from celery import shared_task

from .models import ImageQualityDecision
from .services import process_quality_decision


@shared_task(bind=True, name="quality.analyze_image", acks_late=True)
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
    return {"decision_id": decision_id, "status": result.status, "decision": result.decision}

from celery import shared_task

from .models import DuplicateAnalysis
from .services import process_duplicate_analysis


@shared_task(bind=True, name="duplicates.analyze_image", acks_late=True)
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

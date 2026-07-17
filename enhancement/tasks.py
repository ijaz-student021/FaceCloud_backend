from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import EnhancementBatchJob, EnhancementStatus, ImageEnhancementResult
from .services import process_enhancement_result


@shared_task(bind=True, name="enhancement.process_batch", acks_late=True)
def process_enhancement_batch(self, job_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        job = EnhancementBatchJob.objects.select_for_update().get(pk=job_id)
        if job.status == EnhancementStatus.COMPLETED:
            return {"job_id": job_id, "status": job.status}
        if job.task_id and task_id and job.task_id != task_id:
            return {"job_id": job_id, "status": "superseded"}
        job.status = EnhancementStatus.PROCESSING
        job.started_at = job.started_at or timezone.now()
        job.failure_reason = ""
        job.save(update_fields=["status", "started_at", "failure_reason", "updated_at"])

    result_ids = list(job.results.order_by("pk").values_list("pk", flat=True))
    for result_id in result_ids:
        result = ImageEnhancementResult.objects.get(pk=result_id)
        try:
            process_enhancement_result(result)
        except Exception:
            pass
        successful = job.results.filter(status=EnhancementStatus.COMPLETED).count()
        failed = job.results.filter(status=EnhancementStatus.FAILED).count()
        EnhancementBatchJob.objects.filter(pk=job_id).update(
            processed_images=successful + failed,
            successful_images=successful,
            failed_images=failed,
        )

    successful = job.results.filter(status=EnhancementStatus.COMPLETED).count()
    failed = job.results.filter(status=EnhancementStatus.FAILED).count()
    if successful == job.total_images and job.total_images:
        final_status = EnhancementStatus.COMPLETED
    elif successful:
        final_status = EnhancementStatus.PARTIAL
    else:
        final_status = EnhancementStatus.FAILED
    EnhancementBatchJob.objects.filter(pk=job_id).update(
        status=final_status,
        processed_images=successful + failed,
        successful_images=successful,
        failed_images=failed,
        failure_reason="" if successful else "No image was enhanced successfully.",
        finished_at=timezone.now(),
    )
    return {"job_id": job_id, "status": final_status, "successful": successful, "failed": failed}

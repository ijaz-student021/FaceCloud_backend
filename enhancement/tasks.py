from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import (
    EnhancementBatchJob,
    EnhancementStatus,
    ImageEditVersion,
    ImageEnhancementResult,
)
from .services import process_enhancement_result, process_image_edit


@shared_task(bind=True, name="enhancement.process_image", acks_late=True)
def process_enhancement_image(self, result_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        result = ImageEnhancementResult.objects.select_for_update().get(pk=result_id)
        if result.status in (EnhancementStatus.COMPLETED, EnhancementStatus.CANCELLED):
            return {"result_id": result_id, "status": result.status}
        if result.image.batch.cancellation_requested:
            result.status = EnhancementStatus.CANCELLED
            result.failure_reason = "Processing was cancelled by the user."
            result.finished_at = timezone.now()
            result.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
            return {"result_id": result_id, "status": result.status}
        if result.task_id and task_id and result.task_id != task_id:
            return {"result_id": result_id, "status": "superseded"}
    try:
        result = process_enhancement_result(result)
    except Exception:
        result.refresh_from_db()
        if result.status not in (
            EnhancementStatus.COMPLETED,
            EnhancementStatus.FAILED,
            EnhancementStatus.CANCELLED,
        ):
            result.status = EnhancementStatus.FAILED
            result.failure_reason = "The background enhancement task failed safely."
            result.finished_at = timezone.now()
            result.save(
                update_fields=[
                    "status", "failure_reason", "finished_at", "updated_at",
                ]
            )
        from notifications.events import notify_enhancement_result
        from uploads.progress import sync_image_progress
        notify_enhancement_result(result)
        sync_image_progress(result.image_id)
        return {"result_id": result_id, "status": result.status}
    from notifications.events import notify_enhancement_result
    from uploads.progress import sync_image_progress
    notify_enhancement_result(result)
    sync_image_progress(result.image_id)
    return {"result_id": result_id, "status": result.status}


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
        job.refresh_from_db(fields=["source_batch_id"])
        if job.source_batch.cancellation_requested:
            job.results.exclude(status__in=[EnhancementStatus.COMPLETED, EnhancementStatus.FAILED]).update(
                status=EnhancementStatus.CANCELLED,
                failure_reason="Processing was cancelled by the user.",
                finished_at=timezone.now(),
            )
            EnhancementBatchJob.objects.filter(pk=job_id).update(
                status=EnhancementStatus.CANCELLED, finished_at=timezone.now(),
                failure_reason="Processing was cancelled by the user.",
            )
            return {"job_id": job_id, "status": EnhancementStatus.CANCELLED}
        result = ImageEnhancementResult.objects.get(pk=result_id)
        try:
            process_enhancement_result(result)
        except Exception:
            pass
        result.refresh_from_db()
        from notifications.events import notify_enhancement_result
        from uploads.progress import sync_image_progress
        notify_enhancement_result(result)
        sync_image_progress(result.image_id)
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


@shared_task(bind=True, name="enhancement.process_gallery_edit", acks_late=True)
def process_gallery_edit(self, edit_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        edit = ImageEditVersion.objects.select_for_update().get(pk=edit_id)
        if edit.status in (
            EnhancementStatus.COMPLETED,
            EnhancementStatus.CANCELLED,
        ):
            return {"edit_id": edit_id, "status": edit.status}
        if edit.task_id and task_id and edit.task_id != task_id:
            return {"edit_id": edit_id, "status": "superseded"}
    try:
        edit = process_image_edit(edit)
    except Exception:
        edit.refresh_from_db()
        if edit.status not in (
            EnhancementStatus.COMPLETED,
            EnhancementStatus.FAILED,
            EnhancementStatus.CANCELLED,
        ):
            edit.status = EnhancementStatus.FAILED
            edit.failure_reason = "The background gallery-edit task failed safely."
            edit.finished_at = timezone.now()
            edit.save(
                update_fields=[
                    "status", "failure_reason", "finished_at", "updated_at",
                ]
            )
    return {"edit_id": edit_id, "status": edit.status}

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .services import preprocess_image


@shared_task(bind=True, name="uploads.preprocess_batch", acks_late=True)
def preprocess_batch_task(self, batch_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        batch = UploadBatch.objects.select_for_update().get(pk=batch_id)
        if batch.processing_status == "completed":
            return {"batch_id": batch_id, "status": "completed"}
        if batch.processing_task_id and task_id and batch.processing_task_id != task_id:
            return {"batch_id": batch_id, "status": "superseded"}
        batch.processing_status = "processing"
        batch.processing_started_at = batch.processing_started_at or timezone.now()
        batch.processing_finished_at = None
        batch.processing_error = ""
        batch.save(
            update_fields=[
                "processing_status", "processing_started_at",
                "processing_finished_at", "processing_error", "updated_at",
            ]
        )

    image_ids = list(
        ImageUpload.objects.filter(batch_id=batch_id, status="uploaded")
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for image_id in image_ids:
        image = ImageUpload.objects.get(pk=image_id)
        try:
            preprocess_image(image, requested_format="auto")
        except Exception as error:
            result, created = ImagePreprocessingResult.objects.get_or_create(
                image=image,
                defaults={"owner": image.owner, "requested_format": "auto"},
            )
            if created or result.status != "completed":
                result.status = "failed"
                result.failure_reason = "The image could not be processed by the background worker."
                result.save(update_fields=["status", "failure_reason", "updated_at"])

    completed = ImagePreprocessingResult.objects.filter(
        image__batch_id=batch_id,
        image__status="uploaded",
        status="completed",
    ).count()
    total = len(image_ids)
    failed = max(total - completed, 0)
    if completed == total and total:
        final_status = "completed"
    elif completed:
        final_status = "partial"
    else:
        final_status = "failed"

    UploadBatch.objects.filter(pk=batch_id).update(
        processing_status=final_status,
        processed_files=completed,
        processing_failed_files=failed,
        processing_error="" if final_status != "failed" else "No accepted image was processed successfully.",
        processing_finished_at=timezone.now(),
    )
    return {
        "batch_id": batch_id,
        "status": final_status,
        "processed_files": completed,
        "failed_files": failed,
    }

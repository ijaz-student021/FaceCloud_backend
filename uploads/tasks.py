import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .services import preprocess_image

logger = logging.getLogger(__name__)

# Transient infrastructure failures only (dropped DB connections). Business
# and validation errors are handled per-image and must never retry.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


@shared_task(
    bind=True,
    name="uploads.preprocess_batch",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
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

    from notifications.events import notify_processing_started
    notify_processing_started(batch_id)
    from activity.services import record_activity_event
    record_activity_event(
        batch.owner, "processing", "processing_started",
        object_type="UploadBatch", object_id=batch_id,
    )

    image_ids = list(
        ImageUpload.objects.filter(batch_id=batch_id, status="uploaded")
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    out_of_time = False
    skipped = 0
    for image_id in image_ids:
        if UploadBatch.objects.filter(pk=batch_id, cancellation_requested=True).exists():
            UploadBatch.objects.filter(pk=batch_id).update(
                processing_status="cancelled", processing_finished_at=timezone.now(),
            )
            return {"batch_id": batch_id, "status": "cancelled"}
        image = ImageUpload.all_objects.filter(pk=image_id).first()
        if image is None or image.trashed_at is not None:
            skipped += 1                                            # deleted, or moved to the trash, since this run began: nothing to prepare
            continue
        existing = ImagePreprocessingResult.objects.filter(image=image).first()
        if existing and existing.status == "completed":
            continue
        try:
            preprocess_image(image, requested_format="auto")
        except SoftTimeLimitExceeded:
            # The hard limit follows within minutes: stop here so the batch is
            # recorded as partial instead of being killed mid-image.
            logger.error("Preprocessing of batch %s exceeded the task time limit at image %s.", batch_id, image_id)
            out_of_time = True
            break
        except Exception as error:
            if isinstance(error, ValidationError):
                # An image the preprocessor refused: expected, and already explained.
                logger.warning("Preprocessing of image %s was refused in batch %s: %s", image_id, batch_id, error)
            else:
                logger.exception("Preprocessing of image %s failed in batch %s.", image_id, batch_id)
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
        image__trashed_at__isnull=True,
        status="completed",
    ).count()
    total = len(image_ids) - skipped
    failed = max(total - completed, 0)
    if completed == total and total:
        final_status = "completed"
    elif completed:
        final_status = "partial"
    else:
        final_status = "failed"

    UploadBatch.objects.filter(pk=batch_id, cancellation_requested=False).update(
        processing_status=final_status,
        processed_files=completed,
        processing_failed_files=failed,
        processing_error=(
            "Processing ran out of time before every image was prepared. Retry the batch to finish."
            if out_of_time
            else "" if final_status != "failed" else "No accepted image was processed successfully."
        ),
        processing_finished_at=timezone.now(),
    )
    return {
        "batch_id": batch_id,
        "status": final_status,
        "processed_files": completed,
        "failed_files": failed,
    }


@shared_task(name="uploads.purge_trash", acks_late=True)
def purge_trash_task():
    """Daily: delete for good the photographs that have been in the trash longer than ``TRASH_RETENTION_DAYS``. Safe to run again; it does nothing when the trash is empty."""
    from .trash import purge_expired

    return {"purged": purge_expired()}

"""Starting a batch enhancement job: shared by the owner's batch request and a
moderator's reprocess.

Errors are explicit:

* ``BatchEnhancementConflict`` (409): some images already have an enhancement
  queued or running; their ids are listed. Nothing is created.
* ``BatchEnhancementFailed`` (500): anything unexpected. The full exception is
  logged on the server with a reference; the client gets only the reference.

Creation is atomic, so a failure leaves no job and no half of its results.
"""
import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from .eligibility import not_rejected
from .models import EnhancementBatchJob, EnhancementStatus, ImageEnhancementResult
from .services import create_enhancement_result

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (EnhancementStatus.PENDING, EnhancementStatus.QUEUED, EnhancementStatus.PROCESSING)


class BatchEnhancementConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "enhancement_in_progress"

    def __init__(self, image_ids):
        super().__init__("Some images in this batch already have an enhancement queued or running.")
        # Set after construction: APIException would turn the ids into strings.
        self.detail = {"detail": self.detail, "image_ids": sorted(int(pk) for pk in image_ids)}


class BatchEnhancementFailed(APIException):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_code = "batch_enhancement_failed"

    def __init__(self, reference):
        super().__init__({
            "detail": "The batch enhancement could not be started. Quote the reference if you contact support.",
            "error_reference": reference,
        })


def eligible_images(batch):
    """The batch's photographs that are ready and were not rejected by the checks (see ``enhancement.eligibility``)."""
    return list(
        not_rejected(batch.images.filter(status="uploaded", preprocessing_result__status="completed"))
        .select_related("preprocessing_result", "owner").order_by("pk")
    )


def _busy_image_ids(images):
    return set(
        ImageEnhancementResult.objects.filter(image__in=images, status__in=ACTIVE_STATUSES)
        .values_list("image_id", flat=True)
    )


def start_batch_enhancement(batch, *, level, style, lighting="auto"):
    """Create a job for every eligible image of ``batch`` and queue it.

    The job and its results belong to the batch owner (results must belong to
    the image owner); who asked is for the caller to audit. ``lighting`` (the
    person's own word, ``auto`` by default) is carried by every result of the job.
    """
    from .tasks import process_enhancement_batch

    images = eligible_images(batch)
    if not images:
        raise ValidationError("This batch has no preprocessed images ready for enhancement.")
    busy = _busy_image_ids(images)
    if busy:
        raise BatchEnhancementConflict(busy)
    try:
        with transaction.atomic():
            job = EnhancementBatchJob.objects.create(
                owner=batch.owner, source_batch=batch, requested_level=level, requested_style=style,
                status=EnhancementStatus.PENDING, total_images=len(images),
            )
            for image in images:
                create_enhancement_result(image, level, batch_job=job, requested_style=style, requested_lighting=lighting)
    except IntegrityError as error:
        # Another request started work on one of these images in between.
        logger.warning("Batch %s enhancement raced another request: %s", batch.pk, error)
        raise BatchEnhancementConflict(_busy_image_ids(images)) from error
    except ValidationError:
        raise
    except Exception as error:
        reference = uuid.uuid4().hex[:12]
        logger.exception("Batch %s enhancement could not be created (reference %s).", batch.pk, reference)
        raise BatchEnhancementFailed(reference) from error

    task_id = uuid.uuid4().hex
    job.status = EnhancementStatus.QUEUED
    job.task_id = task_id
    job.save(update_fields=["status", "task_id", "updated_at"])
    try:
        process_enhancement_batch.apply_async(args=[job.pk], task_id=task_id)
    except Exception:
        logger.exception("Batch %s enhancement job %s could not be queued.", batch.pk, job.pk)
        job.status = EnhancementStatus.FAILED
        job.failure_reason = "The background enhancement queue is unavailable."
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
    job.refresh_from_db()
    return job

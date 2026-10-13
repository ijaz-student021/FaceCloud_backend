import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from datetime import timedelta

from django.conf import settings

from .models import (
    EnhancementBatchJob,
    EnhancementStatus,
    ImageEditVersion,
    ImageEnhancementResult,
    ModelComparison,
)
from .comparison import process_model_comparison
from .services import (
    TIME_LIMIT_FAILURE_REASON,
    TRANSIENT_INFRASTRUCTURE_ERRORS,
    EnhancementCancelled,
    EnhancementSuperseded,
    ResultInProgress,
    process_enhancement_result,
    process_image_edit,
)

logger = logging.getLogger(__name__)

# TRANSIENT_INFRASTRUCTURE_ERRORS (dropped DB connections) are the only failures
# worth retrying. Business and validation errors are recorded per result.
TASK_FAILURE_REASON = "The background enhancement task failed safely."
FINISHED_STATUSES = (
    EnhancementStatus.COMPLETED, EnhancementStatus.FAILED, EnhancementStatus.CANCELLED,
)


# How many times a task will wait out another worker's claim before leaving the
# result to that worker. Each wait lasts until the claim would have lapsed.
MAX_CLAIM_WAITS = 2


def _retries_remain(task):
    return task.request.retries < task.max_retries


def _claim_owner(task):
    """This execution's identity for claims: the task id and attempt number."""
    task_id = task.request.id or ""
    return (task_id, task.request.retries) if task_id else None


def _fail_if_unfinished(result, reason=TASK_FAILURE_REASON):
    """Never leave a result looking "in progress" once its task has given up."""
    result.refresh_from_db()
    if result.status in FINISHED_STATUSES:
        return
    result.status = EnhancementStatus.FAILED
    result.failure_reason = reason
    result.claim_token = ""  # nothing is working on it any more
    result.finished_at = timezone.now()
    result.save(update_fields=["status", "failure_reason", "claim_token", "finished_at", "updated_at"])


def _close_job_if_cancelled(job):
    """Close a batch job whose upload batch the user cancelled; True if it was."""
    job.refresh_from_db(fields=["source_batch_id"])
    if not job.source_batch.cancellation_requested:
        return False
    job.results.exclude(status__in=[EnhancementStatus.COMPLETED, EnhancementStatus.FAILED]).update(
        status=EnhancementStatus.CANCELLED,
        failure_reason="Processing was cancelled by the user.",
        claim_token="",
        finished_at=timezone.now(),
    )
    EnhancementBatchJob.objects.filter(pk=job.pk).update(
        status=EnhancementStatus.CANCELLED, finished_at=timezone.now(),
        failure_reason="Processing was cancelled by the user.",
    )
    return True


def _publish_outcome(result):
    """Tell the user, the activity timeline and the progress bar how it ended."""
    from activity.services import record_activity_event
    from notifications.events import notify_enhancement_result
    from uploads.progress import sync_image_progress

    notify_enhancement_result(result)
    record_activity_event(
        result.owner, "processing", f"enhancement_{result.status}",
        object_type="ImageEnhancementResult", object_id=result.pk,
    )
    sync_image_progress(result.image_id)


@shared_task(
    bind=True,
    name="enhancement.process_image",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def process_enhancement_image(self, result_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        result = (
            ImageEnhancementResult.objects.select_for_update()
            .filter(pk=result_id)
            .first()
        )
        if result is None:
            return {"result_id": result_id, "status": "missing"}
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
        result = process_enhancement_result(result, claim_owner=_claim_owner(self))
    except ResultInProgress as busy:
        # Another execution holds a live claim: a duplicate delivery, or a task
        # whose worker died. (This task's own earlier attempt is not "another":
        # its claim is resumed, never waited on.) Stay scheduled and look again
        # once that claim would have lapsed: by then the result is finished, or
        # its worker is gone and this task takes over. The wait is counted
        # separately from database retries, so having retried does not turn a
        # wait into giving up.
        cannot_schedule = self.request.called_directly or self.request.is_eager
        if cannot_schedule or self.request.retries >= self.max_retries + MAX_CLAIM_WAITS:
            logger.warning("Result %s is being enhanced by another worker; leaving it to that worker.", result_id)
            return {"result_id": result_id, "status": "in_progress"}
        raise self.retry(exc=busy, countdown=busy.retry_after, max_retries=self.max_retries + MAX_CLAIM_WAITS)
    except EnhancementSuperseded:
        return {"result_id": result_id, "status": "superseded"}
    except EnhancementCancelled:
        result.refresh_from_db()
        return {"result_id": result_id, "status": result.status}
    except TRANSIENT_INFRASTRUCTURE_ERRORS:
        if _retries_remain(self):
            raise  # autoretry_for schedules the next attempt
        logger.exception("Enhancement of result %s failed after %s retries.", result_id, self.max_retries)
        _fail_if_unfinished(result)
    except SoftTimeLimitExceeded:
        logger.error("Enhancement of result %s exceeded the task time limit.", result_id)
        _fail_if_unfinished(result, TIME_LIMIT_FAILURE_REASON)
    except ValidationError as error:
        # Recorded on the row by the service, which also logs unexpected causes.
        logger.warning("Enhancement of result %s was refused: %s", result_id, error)
        _fail_if_unfinished(result)
    except Exception:
        logger.exception("Enhancement of result %s failed.", result_id)
        _fail_if_unfinished(result)
    _publish_outcome(result)
    return {"result_id": result_id, "status": result.status}


@shared_task(
    bind=True,
    name="enhancement.process_batch",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def process_enhancement_batch(self, job_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        job = (
            EnhancementBatchJob.objects.select_for_update()
            .filter(pk=job_id)
            .first()
        )
        if job is None:
            return {"job_id": job_id, "status": "missing"}
        if job.status == EnhancementStatus.COMPLETED:
            return {"job_id": job_id, "status": job.status}
        if job.task_id and task_id and job.task_id != task_id:
            return {"job_id": job_id, "status": "superseded"}
        job.status = EnhancementStatus.PROCESSING
        job.started_at = job.started_at or timezone.now()
        job.failure_reason = ""
        job.save(update_fields=["status", "started_at", "failure_reason", "updated_at"])

    result_ids = list(job.results.order_by("pk").values_list("pk", flat=True))
    out_of_time = False
    for result_id in result_ids:
        if _close_job_if_cancelled(job):
            return {"job_id": job_id, "status": EnhancementStatus.CANCELLED}
        result = ImageEnhancementResult.objects.get(pk=result_id)
        try:
            process_enhancement_result(result, claim_owner=_claim_owner(self))
        except (ResultInProgress, EnhancementSuperseded):
            # Another worker owns this image (a duplicate delivery of this batch).
            # It will record the outcome; do not fail or report it from here.
            logger.warning("Result %s is owned by another worker; batch job %s skipped it.", result_id, job_id)
            continue
        except EnhancementCancelled:
            continue  # the cancellation check at the top of the loop closes the job
        except TRANSIENT_INFRASTRUCTURE_ERRORS:
            if _retries_remain(self):
                raise  # the retried batch skips what already completed
            logger.exception(
                "Enhancement of result %s failed inside batch job %s after %s retries.",
                result_id, job_id, self.max_retries,
            )
        except SoftTimeLimitExceeded:
            # The hard limit follows within minutes: stop now, while there is
            # still time to record what happened, instead of starting more images.
            out_of_time = True
            logger.error(
                "Batch job %s exceeded the task time limit at result %s.", job_id, result_id,
            )
        except ValidationError as error:
            logger.warning(
                "Enhancement of result %s was refused inside batch job %s: %s", result_id, job_id, error,
            )
        except Exception:
            # One bad image must not stop the batch, but it must leave a trace and
            # must not be left looking "in progress".
            logger.exception("Enhancement of result %s failed inside batch job %s.", result_id, job_id)
        _fail_if_unfinished(result, TIME_LIMIT_FAILURE_REASON if out_of_time else TASK_FAILURE_REASON)
        _publish_outcome(result)
        if out_of_time:
            for unfinished in job.results.exclude(status__in=FINISHED_STATUSES):
                _fail_if_unfinished(unfinished, TIME_LIMIT_FAILURE_REASON)
                _publish_outcome(unfinished)
        successful = job.results.filter(status=EnhancementStatus.COMPLETED).count()
        failed = job.results.filter(status=EnhancementStatus.FAILED).count()
        EnhancementBatchJob.objects.filter(pk=job_id).update(
            processed_images=successful + failed,
            successful_images=successful,
            failed_images=failed,
        )
        if out_of_time:
            break

    # A cancellation that arrived during the last image has no next iteration
    # to notice it.
    if _close_job_if_cancelled(job):
        return {"job_id": job_id, "status": EnhancementStatus.CANCELLED}

    successful = job.results.filter(status=EnhancementStatus.COMPLETED).count()
    failed = job.results.filter(status=EnhancementStatus.FAILED).count()
    if successful == job.total_images and job.total_images:
        final_status = EnhancementStatus.COMPLETED
    elif successful:
        final_status = EnhancementStatus.PARTIAL
    else:
        final_status = EnhancementStatus.FAILED
    if out_of_time:
        failure_reason = (
            "The batch ran out of time before every image was enhanced. "
            "Start it again to process the remaining images."
        )
    else:
        failure_reason = "" if successful else "No image was enhanced successfully."
    EnhancementBatchJob.objects.filter(pk=job_id).update(
        status=final_status,
        processed_images=successful + failed,
        successful_images=successful,
        failed_images=failed,
        failure_reason=failure_reason,
        finished_at=timezone.now(),
    )
    return {"job_id": job_id, "status": final_status, "successful": successful, "failed": failed}


@shared_task(
    bind=True,
    name="enhancement.process_gallery_edit",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def process_gallery_edit(self, edit_id):
    task_id = self.request.id or ""
    with transaction.atomic():
        edit = (
            ImageEditVersion.objects.select_for_update()
            .filter(pk=edit_id)
            .first()
        )
        if edit is None:
            return {"edit_id": edit_id, "status": "missing"}
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
        logger.exception("Gallery edit %s failed.", edit_id)
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


@shared_task(
    bind=True,
    name="enhancement.run_model_comparison",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def run_model_comparison(self, comparison_id):
    task_id = self.request.id or ""
    comparison = ModelComparison.objects.filter(pk=comparison_id).first()
    if comparison is None:
        return {"comparison_id": comparison_id, "status": "deleted"}
    if comparison.task_id and task_id and comparison.task_id != task_id:
        return {"comparison_id": comparison_id, "status": "superseded"}
    comparison = process_model_comparison(comparison)
    return {"comparison_id": comparison_id, "status": comparison.status}


@shared_task(
    name="enhancement.cleanup_comparisons",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_old_comparisons():
    """Delete finished comparisons (and their preview images) past the retention period."""
    cutoff = timezone.now() - timedelta(days=settings.COMPARISON_RETENTION_DAYS)
    finished = (EnhancementStatus.COMPLETED, EnhancementStatus.FAILED, EnhancementStatus.CANCELLED)
    _, per_model = ModelComparison.objects.filter(
        status__in=finished, created_at__lt=cutoff,
    ).delete()
    return {"deleted_comparisons": per_model.get("enhancement.ModelComparison", 0)}

import logging
import uuid

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.time import get_exponential_backoff_interval
from django.db import transaction
from django.db.models import Q
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
    EditInProgress,
    EditSuperseded,
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
#
# Claim waits and database retries are separate budgets. Celery counts both in
# ``request.retries``, so a task passes the number of waits so far as the
# ``claim_waits`` argument; the rest of its retries were for database failures
# (``max_retries`` of them are allowed).
MAX_CLAIM_WAITS = 2


def _database_retries(task, claim_waits):
    return max(0, task.request.retries - claim_waits)


def _database_retries_remain(task, claim_waits):
    return _database_retries(task, claim_waits) < task.max_retries


def _retry_after_database_error(task, error, claim_waits, **kwargs):
    """Schedule the next attempt after a dropped database connection.

    ``kwargs`` are the next attempt's task arguments, including the claim this
    execution held, so the retry resumes it instead of waiting for it to lapse.
    """
    countdown = get_exponential_backoff_interval(
        factor=1, retries=_database_retries(task, claim_waits),
        maximum=settings.TASK_RETRY_BACKOFF_MAX_SECONDS, full_jitter=True,
    )
    raise task.retry(
        exc=error, countdown=countdown, kwargs={"claim_waits": claim_waits, **kwargs},
        max_retries=task.max_retries + MAX_CLAIM_WAITS,
    )


def _claim_owner(task, claim_waits):
    """This execution's identity, recorded with its claim: task id and database attempt."""
    task_id = task.request.id or ""
    return (task_id, _database_retries(task, claim_waits)) if task_id else None


def _fail_if_unfinished(result, reason=TASK_FAILURE_REASON, token=None):
    """Never leave a result looking "in progress" once its task has given up.

    Only a row this execution may close is failed: one it holds (``token``), or
    one nobody holds -- unclaimed, or a claim that was not renewed within the
    lease. A live claim belongs to another execution, which records its own
    outcome. Returns whether the row is now finished.
    """
    lapsed = timezone.now() - timedelta(seconds=settings.ENHANCEMENT_CLAIM_LEASE_SECONDS)
    closable = Q(claim_token="") | Q(claimed_at__isnull=True) | Q(claimed_at__lt=lapsed)
    if token:
        closable |= Q(claim_token=token)
    with transaction.atomic():
        current = (
            ImageEnhancementResult.objects.select_for_update()
            .filter(pk=result.pk).exclude(status__in=FINISHED_STATUSES).filter(closable).first()
        )
        if current is not None:
            current.status = EnhancementStatus.FAILED
            current.failure_reason = reason
            current.claim_token = ""  # nothing is working on it any more
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "failure_reason", "claim_token", "finished_at", "updated_at"])
    result.refresh_from_db()
    if current is None and result.status not in FINISHED_STATUSES:
        logger.warning("Result %s is held by another execution; leaving its outcome to that execution.", result.pk)
    return result.status in FINISHED_STATUSES


def _close_job_if_cancelled(job):
    """Close a batch job whose upload batch the user cancelled; True if it was.

    A cancellation that arrives once every image has completed or failed has
    nothing left to cancel: the job keeps its real outcome (completed, partial
    or failed) and its counts. One that stopped an image (cancelled it) or has
    images left to stop closes the job as cancelled. Progress for the cancelled
    images is set by ``uploads.services.cancel_batch`` when the cancellation is
    made.
    """
    job.refresh_from_db(fields=["source_batch_id"])
    if not job.source_batch.cancellation_requested:
        return False
    if not job.results.exclude(status__in=(EnhancementStatus.COMPLETED, EnhancementStatus.FAILED)).exists():
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
    """Tell the user, the activity timeline and the progress bar how it ended.

    Only a finished outcome is published, and publishing it again (a retried
    task) adds nothing: notifications carry idempotency keys, and an activity
    entry for this outcome recorded since the result finished is not repeated.
    A later, different outcome of the same result is still recorded.
    """
    from activity.models import ActivityEvent
    from activity.services import record_activity_event
    from notifications.events import notify_enhancement_result
    from uploads.progress import sync_image_progress

    if result.status not in FINISHED_STATUSES:
        return
    notify_enhancement_result(result)
    event = f"enhancement_{result.status}"
    already = ActivityEvent.objects.filter(
        user=result.owner, category="processing", event=event,
        object_type="ImageEnhancementResult", object_id=str(result.pk),
    )
    if result.finished_at:
        already = already.filter(created_at__gte=result.finished_at)
    if not already.exists():
        record_activity_event(
            result.owner, "processing", event,
            object_type="ImageEnhancementResult", object_id=result.pk,
        )
    sync_image_progress(result.image_id)


@shared_task(
    bind=True,
    name="enhancement.process_image",
    acks_late=True,
    max_retries=3,  # database failures; claim waits are counted separately
)
def process_enhancement_image(self, result_id, claim_waits=0, resume_claim=""):
    try:
        return _enhance_image(self, result_id, claim_waits, resume_claim)
    except TRANSIENT_INFRASTRUCTURE_ERRORS as error:
        # The database went away outside the enhancement itself (loading or
        # closing the row). No claim was taken, or the one taken is passed on.
        if _database_retries_remain(self, claim_waits):
            _retry_after_database_error(self, error, claim_waits, resume_claim=resume_claim)
        raise


def _enhance_image(task, result_id, claim_waits, resume_claim):
    task_id = task.request.id or ""
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
    token = uuid.uuid4().hex
    try:
        result = process_enhancement_result(
            result, claim_owner=_claim_owner(task, claim_waits), token=token, resume_token=resume_claim,
        )
        finished = True
    except ResultInProgress as busy:
        # Another execution holds a live claim: a duplicate delivery, or a task
        # whose worker died. Stay scheduled and look again once that claim would
        # have lapsed: by then the result is finished, or its worker is gone and
        # this task takes over. A live execution keeps renewing its claim, so
        # after MAX_CLAIM_WAITS this task leaves the result to it; if that worker
        # dies later, its own unacknowledged message is delivered again.
        cannot_schedule = task.request.called_directly or task.request.is_eager
        if cannot_schedule or claim_waits >= MAX_CLAIM_WAITS:
            logger.warning("Result %s is being enhanced by another worker; leaving it to that worker.", result_id)
            return {"result_id": result_id, "status": "in_progress"}
        raise task.retry(
            exc=busy, countdown=busy.retry_after,
            kwargs={"claim_waits": claim_waits + 1, "resume_claim": resume_claim},
            max_retries=task.max_retries + MAX_CLAIM_WAITS,
        )
    except EnhancementSuperseded:
        return {"result_id": result_id, "status": "superseded"}
    except EnhancementCancelled:
        result.refresh_from_db()
        return {"result_id": result_id, "status": result.status}
    except TRANSIENT_INFRASTRUCTURE_ERRORS as error:
        if _database_retries_remain(task, claim_waits):
            _retry_after_database_error(task, error, claim_waits, resume_claim=token)
        logger.exception("Enhancement of result %s failed after %s retries.", result_id, task.max_retries)
        finished = _fail_if_unfinished(result, token=token)
    except SoftTimeLimitExceeded:
        logger.error("Enhancement of result %s exceeded the task time limit.", result_id)
        finished = _fail_if_unfinished(result, TIME_LIMIT_FAILURE_REASON, token)
    except ValidationError as error:
        # Recorded on the row by the service, which also logs unexpected causes.
        logger.warning("Enhancement of result %s was refused: %s", result_id, error)
        finished = _fail_if_unfinished(result, token=token)
    except Exception:
        logger.exception("Enhancement of result %s failed.", result_id)
        finished = _fail_if_unfinished(result, token=token)
    if finished:
        _publish_outcome(result)
    return {"result_id": result_id, "status": result.status}


@shared_task(
    bind=True,
    name="enhancement.process_batch",
    acks_late=True,
    max_retries=3,  # database failures; claim waits are counted separately
)
def process_enhancement_batch(self, job_id, claim_waits=0, resume_claims=None):
    resume_claims = resume_claims or {}
    try:
        return _enhance_batch(self, job_id, claim_waits, resume_claims)
    except TRANSIENT_INFRASTRUCTURE_ERRORS as error:
        if _database_retries_remain(self, claim_waits):
            _retry_after_database_error(self, error, claim_waits, resume_claims=resume_claims)
        raise


def _enhance_batch(task, job_id, claim_waits, resume_claims):
    """Enhance a batch job's images. ``resume_claims`` maps result id (as a
    string) to the claim this task's previous execution held on it."""
    task_id = task.request.id or ""
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
    held_elsewhere = []  # how long until each claim held by another execution lapses
    for result_id in result_ids:
        if _close_job_if_cancelled(job):
            return {"job_id": job_id, "status": EnhancementStatus.CANCELLED}
        result = ImageEnhancementResult.objects.get(pk=result_id)
        if result.status in FINISHED_STATUSES:
            # Finished by an earlier delivery or attempt of this batch: neither
            # redone nor reported again.
            continue
        token = uuid.uuid4().hex
        try:
            process_enhancement_result(
                result, claim_owner=_claim_owner(task, claim_waits), token=token,
                resume_token=resume_claims.get(str(result_id), ""),
            )
        except ResultInProgress as busy:
            # Another execution holds this image: a duplicate delivery of this
            # batch, or this batch's earlier delivery whose worker died mid-image.
            # Come back for it once that claim would have lapsed (below).
            held_elsewhere.append(busy.retry_after)
            continue
        except EnhancementSuperseded:
            logger.warning("Result %s was taken over by another worker; batch job %s left it.", result_id, job_id)
            continue
        except EnhancementCancelled:
            continue  # the cancellation check at the top of the loop closes the job
        except TRANSIENT_INFRASTRUCTURE_ERRORS as error:
            if _database_retries_remain(task, claim_waits):
                # The retried batch skips what already finished and resumes this image.
                _retry_after_database_error(
                    task, error, claim_waits, resume_claims={**resume_claims, str(result_id): token},
                )
            logger.exception(
                "Enhancement of result %s failed inside batch job %s after %s retries.",
                result_id, job_id, task.max_retries,
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
        if _fail_if_unfinished(result, TIME_LIMIT_FAILURE_REASON if out_of_time else TASK_FAILURE_REASON, token):
            _publish_outcome(result)
        if out_of_time:
            for unfinished in job.results.exclude(status__in=FINISHED_STATUSES):
                if _fail_if_unfinished(unfinished, TIME_LIMIT_FAILURE_REASON):
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

    if held_elsewhere and not out_of_time:
        cannot_schedule = task.request.called_directly or task.request.is_eager
        if not cannot_schedule and claim_waits < MAX_CLAIM_WAITS:
            raise task.retry(
                countdown=max(held_elsewhere),
                kwargs={"claim_waits": claim_waits + 1, "resume_claims": resume_claims},
                max_retries=task.max_retries + MAX_CLAIM_WAITS,
            )
        logger.warning(
            "Batch job %s finished without %s image(s) another worker is still enhancing.",
            job_id, len(held_elsewhere),
        )

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
    max_retries=3,  # database failures
)
def process_gallery_edit(self, edit_id, resume_claim=""):
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
    token = uuid.uuid4().hex
    try:
        edit = process_image_edit(edit, token=token, resume_token=resume_claim)
    except EditInProgress:
        logger.warning("Gallery edit %s is being rendered by another worker; leaving it to that worker.", edit_id)
        return {"edit_id": edit_id, "status": "in_progress"}
    except EditSuperseded:
        return {"edit_id": edit_id, "status": "superseded"}
    except TRANSIENT_INFRASTRUCTURE_ERRORS as error:
        if _database_retries_remain(self, 0):
            _retry_after_database_error(self, error, 0, resume_claim=token)
        logger.exception("Gallery edit %s failed after %s retries.", edit_id, self.max_retries)
        _fail_edit_if_owner(edit_id, token)
    except Exception:
        # Recorded on the row by the service, which also logs unexpected causes.
        logger.warning("Gallery edit %s failed.", edit_id)
    edit = ImageEditVersion.objects.filter(pk=edit_id).first()
    return {"edit_id": edit_id, "status": edit.status if edit else "missing"}


def _fail_edit_if_owner(edit_id, token):
    """Close an edit as failed, but only if this execution still holds its claim."""
    ImageEditVersion.objects.filter(pk=edit_id, claim_token=token).update(
        status=EnhancementStatus.FAILED, claim_token="", finished_at=timezone.now(),
        failure_reason="The background gallery-edit task failed safely.",
    )


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


@shared_task(
    name="enhancement.cleanup_previews",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_edit_previews():
    """Delete expired editor previews and unused cached sources.

    Previews are disposable and have their own retention
    (ENHANCEMENT_PREVIEW_RETENTION_HOURS); originals, enhanced results and saved
    edit versions are never touched here.
    """
    from .previews import cleanup_expired

    return cleanup_expired()


@shared_task(
    bind=True,
    name="enhancement.advisor_recommend",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=200,  # database errors and waits behind the concurrency limit; the job's own deadline ends it sooner
)
def advisor_recommend(self, recommendation_id):
    """Produce the cloud advisor's suggestion for ONE image (see advisor_service.py).

    Each image is its own job, so one failing never fails another. A wait behind the
    concurrency limit or a transient provider error re-queues this job; its deadline,
    capped attempts and the daily budgets bound the total work and spend.
    """
    from .advisor_service import RetryLater, run_recommendation

    try:
        status = run_recommendation(recommendation_id)
    except RetryLater as wait:
        raise self.retry(countdown=wait.countdown, exc=wait) from None
    return {"recommendation_id": recommendation_id, "status": status}


@shared_task(
    name="enhancement.cleanup_advisor",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_advisor_records():
    """Close advisor jobs no worker finished, and delete old records (counts and codes only; no images are kept)."""
    from .advisor_service import cleanup_records

    return cleanup_records()

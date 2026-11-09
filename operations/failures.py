"""What the administrator's failure screens show about a failed batch or job, in plain words.

Shared by ``GET /api/system/batches/failed/``, ``GET /api/system/jobs/failed/`` and the answer to ``POST /api/system/batches/{id}/retry/``, so one failed
batch reads the same everywhere. Nothing here is measured or stored: it only reads what the pipeline already recorded.

* The **stage** a batch failed at is where its failed photographs failed (``BatchImageProgress.failure_stage``), not ``UploadBatch.current_stage``: once
  every photograph has finished that is ``completed``, whatever happened to them.
* The **reason** is a sentence the pipeline wrote ("The stored image could not be safely preprocessed."), never an exception text. When none was recorded a
  sentence is made from the stage, so the field is never empty.
* The **organization** is the one the batch was uploaded to; a job with no batch, or a batch that was never filed under one, shows the owner's own
  workspace (their personal organization, else their oldest active membership), the same default new uploads get.
"""
from collections import Counter

from django.conf import settings

from organizations.models import OrganizationMembership
from uploads.models import BatchImageProgress, UploadBatch
from uploads.services import retry_obstacle

REASON_LIMIT = 500
STAGE_LABELS = dict(UploadBatch.STAGE_CHOICES)
STAGE_ORDER = [key for key, _label in UploadBatch.STAGE_CHOICES]


def stage_label(stage):
    return STAGE_LABELS.get(stage) or (stage or "").replace("_", " ").capitalize()


def owner_workspace(owner_id, cache):
    """``(organization_id, organization_name)`` of the owner's own workspace, or ``(None, None)``; ``cache`` keeps one lookup per owner."""
    if owner_id not in cache:
        membership = (
            OrganizationMembership.objects.select_related("organization")
            .filter(user_id=owner_id, status=OrganizationMembership.Status.ACTIVE)
            .order_by("-organization__is_personal", "created_at")
            .first()
        )
        cache[owner_id] = (membership.organization_id, membership.organization.name) if membership else (None, None)
    return cache[owner_id]


def organization_of(batch, owner_id, cache):
    """The organization a failed row belongs to: its batch's, else the owner's own workspace."""
    if batch is not None and batch.organization_id:
        return batch.organization_id, batch.organization.name
    return owner_workspace(owner_id, cache)


def failed_images_of(batch):
    """The batch's failed photographs; uses ``batch.failed_progress`` when the caller prefetched it."""
    prefetched = getattr(batch, "failed_progress", None)
    if prefetched is not None:
        return list(prefetched)
    return list(BatchImageProgress.objects.filter(batch=batch, status="failed").order_by("image_id"))


def _failure_stage(batch, failed):
    stages = Counter(item.failure_stage for item in failed if item.failure_stage)
    if stages:
        # The stage most photographs failed at; on a tie the earlier one in the pipeline (the later ones may only be a consequence).
        return sorted(stages, key=lambda stage: (-stages[stage], STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)))[0]
    # Nothing failed photograph-by-photograph (the whole batch failed before or while starting): where it had got to, unless it says "done".
    return batch.current_stage if batch.current_stage not in ("", "completed") else ""


def _failure_reason(batch, failed, stage):
    reasons = Counter((item.failure_reason or "").strip() for item in failed if (item.failure_reason or "").strip())
    if reasons:
        return reasons.most_common(1)[0][0][:REASON_LIMIT]
    recorded = (batch.processing_error or "").strip()
    if recorded:
        return recorded[:REASON_LIMIT]
    return f"{stage_label(stage)} did not finish." if stage else "The batch failed, but no reason was recorded."


def describe_failed_batch(batch):
    """Stage, reason, time and retry state of a failed batch (the keys the failure screens add to a batch row)."""
    failed = failed_images_of(batch)
    stage = _failure_stage(batch, failed)
    finished = [item.finished_at for item in failed if item.finished_at]
    obstacle = retry_obstacle(batch, len(failed))
    maximum = getattr(settings, "BATCH_PROGRESS_MAX_RETRIES", 3)
    return {
        "failure_stage": stage,
        "failure_stage_label": stage_label(stage),
        "failure_reason": _failure_reason(batch, failed, stage),
        "failed_images": len(failed),
        "failed_at": max(finished) if finished else (batch.processing_finished_at or batch.updated_at),
        "can_retry": obstacle is None,
        "retry_blocked_reason": "" if obstacle is None else " ".join(str(item) for item in obstacle.detail),
        "max_retries": maximum,
        "retries_left": max(0, maximum - batch.retry_count),
    }

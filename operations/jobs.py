"""Detection and recovery of background jobs that will never finish.

A worker that dies, or a broker that loses its queue, leaves a database row in
"queued" or "processing" forever. Besides misreporting progress, such a row
blocks the user: the app allows one active operation per image or package, so
they cannot simply try again. This module finds those rows and fails them so
the work can be retried, and reports queue health for administrators.

Two ages decide what is stale:

* "processing" rows: a task cannot run past CELERY_TASK_TIME_LIMIT, so a row
  that has not changed for that long plus a grace period has no live task.
* "queued" rows: they can legitimately wait behind a backlog, so they get a much
  longer, separately configured age.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.db.models import Min
from django.utils import timezone

logger = logging.getLogger(__name__)

STALE_REASON = (
    "The background worker stopped responding, so this job was marked failed "
    "automatically. You can start it again."
)


@dataclass(frozen=True)
class JobKind:
    key: str
    model: str
    queued: tuple
    processing: tuple
    failed: str
    status_field: str = "status"
    reason_field: str = "failure_reason"
    finished_field: str | None = "finished_at"
    # Attribute holding the ImageUpload id, so batch progress can be re-synced.
    image_field: str | None = None
    # How the failure screens name this kind of work, and the pipeline stage it is (a batch works it out from its photographs).
    label: str = ""
    stage: str = ""
    # Lookup from a row to the UploadBatch it belongs to (the row itself when ""), so its organization can be shown; None: no batch.
    batch_path: str | None = None

    def model_class(self):
        return apps.get_model(self.model)

    @property
    def active(self):
        return self.queued + self.processing


JOB_KINDS = (
    JobKind(
        "enhancement", "enhancement.ImageEnhancementResult",
        queued=("pending", "queued"), processing=("processing",),
        failed="failed", image_field="image_id", label="Enhancement", stage="enhancement", batch_path="image__batch",
    ),
    JobKind(
        "enhancement_batch", "enhancement.EnhancementBatchJob",
        queued=("pending", "queued"), processing=("processing",), failed="failed",
        label="Enhancement of a whole batch", stage="enhancement", batch_path="source_batch",
    ),
    JobKind(
        "model_comparison", "enhancement.ModelComparison",
        queued=("pending", "queued"), processing=("processing",), failed="failed",
        label="Model comparison", stage="model_comparison", batch_path="image__batch",
    ),
    JobKind(
        "gallery_edit", "enhancement.ImageEditVersion",
        queued=("pending", "queued"), processing=("processing",), failed="failed",
        label="Gallery edit", stage="gallery_edit", batch_path="enhancement_result__image__batch",
    ),
    JobKind(
        "identity_analysis", "identities.IdentityAnalysis",
        queued=("queued",), processing=("processing",),
        failed="failed", image_field="image_id", label="Identity analysis", stage="identity_grouping", batch_path="image__batch",
    ),
    JobKind(
        "preprocessing", "uploads.ImagePreprocessingResult",
        queued=(), processing=("processing",), failed="failed",
        finished_field=None, label="Preprocessing", stage="preprocessing", batch_path="image__batch",
    ),
    JobKind(
        "upload_batch", "uploads.UploadBatch",
        queued=("queued",), processing=("processing",), failed="failed",
        status_field="processing_status", reason_field="processing_error",
        finished_field="processing_finished_at", label="Upload batch", batch_path="",
    ),
    JobKind(
        "download_package", "downloads.DownloadPackage",
        queued=("queued",), processing=("generating",), failed="failed",
        finished_field=None, label="Download package", stage="download_package", batch_path="batch",
    ),
    JobKind(
        "report", "reports.ProcessingReport",
        queued=("queued",), processing=("generating",), failed="failed",
        finished_field=None, label="Processing report", stage="report", batch_path="batch",
    ),
    JobKind(
        "identity_export", "identities.IdentityGroupExport",
        queued=("queued",), processing=("generating",), failed="failed",
        finished_field=None, label="Identity export", stage="identity_export",
    ),
)


def processing_stale_age():
    return timedelta(seconds=settings.JOB_PROCESSING_STALE_SECONDS)


def queued_stale_age():
    return timedelta(hours=settings.JOB_QUEUED_STALE_HOURS)


def _stale_filters(kind, now, processing_seconds=None):
    """Two (statuses, cutoff) pairs describing stale queued and processing rows."""
    processing_age = timedelta(seconds=processing_seconds) if processing_seconds is not None else processing_stale_age()
    return (
        (kind.queued, now - queued_stale_age()),
        (kind.processing, now - processing_age),
    )


def reap_stuck_jobs(now=None, dry_run=False, processing_seconds=None):
    """Fail every job whose worker is gone; returns counts per job kind.

    Each update re-applies the staleness filter in one statement, so a job that
    finishes (or is touched) while this runs is left alone. ``processing_seconds`` replaces
    JOB_PROCESSING_STALE_SECONDS for one run: after restarting a development server, whatever it was
    processing is gone, and waiting the default (over an hour) is pointless.
    """
    from uploads.progress import sync_image_progress

    now = now or timezone.now()
    summary = {}
    for kind in JOB_KINDS:
        model = kind.model_class()
        reaped = 0
        image_ids = set()
        for statuses, cutoff in _stale_filters(kind, now, processing_seconds):
            if not statuses:
                continue
            stale = model.objects.filter(
                **{f"{kind.status_field}__in": statuses, "updated_at__lt": cutoff}
            )
            if dry_run:
                reaped += stale.count()
                continue
            if kind.image_field:
                image_ids.update(stale.values_list(kind.image_field, flat=True))
            changes = {
                kind.status_field: kind.failed,
                kind.reason_field: STALE_REASON,
                "updated_at": now,
            }
            if kind.finished_field:
                changes[kind.finished_field] = now
            reaped += stale.update(**changes)
        if reaped:
            summary[kind.key] = reaped
            if not dry_run:
                logger.warning("Failed %d stuck %s job(s).", reaped, kind.key)
        for image_id in image_ids:
            sync_image_progress(image_id)
    return {"dry_run": dry_run, "total": sum(summary.values()), "by_kind": summary}


def queue_status(now=None):
    """Read-only snapshot of active work per job kind for administrators."""
    now = now or timezone.now()
    kinds = {}
    for kind in JOB_KINDS:
        model = kind.model_class()
        rows = model.objects.filter(**{f"{kind.status_field}__in": kind.active})
        oldest = rows.aggregate(oldest=Min("created_at"))["oldest"]
        (queued_statuses, queued_cutoff), (processing_statuses, processing_cutoff) = (
            _stale_filters(kind, now)
        )
        stale = 0
        if queued_statuses:
            stale += rows.filter(
                **{f"{kind.status_field}__in": queued_statuses, "updated_at__lt": queued_cutoff}
            ).count()
        if processing_statuses:
            stale += rows.filter(
                **{
                    f"{kind.status_field}__in": processing_statuses,
                    "updated_at__lt": processing_cutoff,
                }
            ).count()
        kinds[kind.key] = {
            "queued": rows.filter(**{f"{kind.status_field}__in": kind.queued}).count()
            if kind.queued else 0,
            "processing": rows.filter(**{f"{kind.status_field}__in": kind.processing}).count(),
            "stale": stale,
            "oldest_active_age_seconds": (
                int((now - oldest).total_seconds()) if oldest else None
            ),
            "failed_total": model.objects.filter(**{kind.status_field: kind.failed}).count(),
        }
    return {
        "generated_at": now,
        "processing_stale_after_seconds": settings.JOB_PROCESSING_STALE_SECONDS,
        "queued_stale_after_hours": settings.JOB_QUEUED_STALE_HOURS,
        "jobs": kinds,
    }


DEFAULT_FAILED_JOBS_LIMIT_PER_KIND = 20
MAX_FAILED_JOBS_LIMIT_PER_KIND = 100
JOB_KIND_KEYS = tuple(job_kind.key for job_kind in JOB_KINDS)


class InvalidJobKind(ValueError):
    """Raised for an unrecognised ``kind`` filter; carries a user-facing message."""


def _batch_of(row, job_kind):
    """The UploadBatch a failed row belongs to, or None (a kind with no batch, or a row whose link is empty)."""
    if job_kind.batch_path is None:
        return None
    target = row
    for part in filter(None, job_kind.batch_path.split("__")):
        target = getattr(target, part, None)
        if target is None:
            return None
    return target


def _failed_job_row(row, job_kind, workspaces):
    """One failed job as the administrator's screen shows it: the original keys, then where, why and when, in plain words."""
    from .failures import describe_failed_batch, organization_of, stage_label

    batch = _batch_of(row, job_kind)
    organization_id, organization_name = organization_of(batch, row.owner_id, workspaces)
    stored = getattr(row, job_kind.reason_field, "") or ""
    finished = getattr(row, job_kind.finished_field, None) if job_kind.finished_field else None
    entry = {
        "id": row.pk,
        "owner_id": row.owner_id,
        "owner_username": row.owner.username,
        "reason": stored,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "kind": job_kind.key,
        "kind_label": job_kind.label,
        "organization_id": organization_id,
        "organization_name": organization_name,
        "failure_stage": job_kind.stage,
        "failure_stage_label": stage_label(job_kind.stage),
        "failure_reason": stored.strip()[:500] or f"{job_kind.label} failed, but no reason was recorded.",
        "failed_at": finished or row.updated_at,
    }
    if job_kind.key == "upload_batch":
        entry.update(describe_failed_batch(row))
    return entry


def list_failed_jobs(*, kind=None, owner=None, limit_per_kind=DEFAULT_FAILED_JOBS_LIMIT_PER_KIND):
    """The most recent failed row of each job kind, for administrator visibility.

    Read-only: this surfaces what failed so an admin (or the affected user, via
    the owner-facing retry/support flow) can act on it. ``kind`` restricts to one
    job kind by its ``JobKind.key``; ``owner`` restricts to one user's id.

    Every row carries who (``owner_*``), which organization (``organization_*``: its batch's, else the owner's own workspace), what
    (``kind``, ``kind_label``), where it failed (``failure_stage`` and its label), why (``failure_reason``, always a sentence; ``reason`` is the stored
    text, which may be empty) and when (``failed_at``). A failed upload batch also says whether it can be retried now, like the failed-batch list.
    """
    if kind is not None and kind not in JOB_KIND_KEYS:
        raise InvalidJobKind(f"Unknown job kind '{kind}'. Choose one of: {', '.join(JOB_KIND_KEYS)}.")
    limit = max(1, min(int(limit_per_kind), MAX_FAILED_JOBS_LIMIT_PER_KIND))
    kinds = [candidate for candidate in JOB_KINDS if kind is None or candidate.key == kind]
    results = {}
    workspaces = {}
    for job_kind in kinds:
        model = job_kind.model_class()
        rows = model.objects.filter(**{job_kind.status_field: job_kind.failed})
        if owner is not None:
            rows = rows.filter(owner_id=owner)
        rows = rows.select_related("owner")
        if job_kind.batch_path is not None:
            organization_path = f"{job_kind.batch_path}__organization" if job_kind.batch_path else "organization"
            rows = rows.select_related(organization_path)
        rows = rows.order_by("-updated_at")[:limit]
        results[job_kind.key] = [_failed_job_row(row, job_kind, workspaces) for row in rows]
    return {
        "generated_at": timezone.now(),
        "limit_per_kind": limit,
        "kinds": results,
    }

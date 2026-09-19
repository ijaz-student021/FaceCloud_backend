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

    def model_class(self):
        return apps.get_model(self.model)

    @property
    def active(self):
        return self.queued + self.processing


JOB_KINDS = (
    JobKind(
        "enhancement", "enhancement.ImageEnhancementResult",
        queued=("pending", "queued"), processing=("processing",),
        failed="failed", image_field="image_id",
    ),
    JobKind(
        "enhancement_batch", "enhancement.EnhancementBatchJob",
        queued=("pending", "queued"), processing=("processing",), failed="failed",
    ),
    JobKind(
        "model_comparison", "enhancement.ModelComparison",
        queued=("pending", "queued"), processing=("processing",), failed="failed",
    ),
    JobKind(
        "gallery_edit", "enhancement.ImageEditVersion",
        queued=("pending", "queued"), processing=("processing",), failed="failed",
    ),
    JobKind(
        "identity_analysis", "identities.IdentityAnalysis",
        queued=("queued",), processing=("processing",),
        failed="failed", image_field="image_id",
    ),
    JobKind(
        "preprocessing", "uploads.ImagePreprocessingResult",
        queued=(), processing=("processing",), failed="failed",
        finished_field=None,
    ),
    JobKind(
        "upload_batch", "uploads.UploadBatch",
        queued=("queued",), processing=("processing",), failed="failed",
        status_field="processing_status", reason_field="processing_error",
        finished_field="processing_finished_at",
    ),
    JobKind(
        "download_package", "downloads.DownloadPackage",
        queued=("queued",), processing=("generating",), failed="failed",
        finished_field=None,
    ),
    JobKind(
        "report", "reports.ProcessingReport",
        queued=("queued",), processing=("generating",), failed="failed",
        finished_field=None,
    ),
    JobKind(
        "identity_export", "identities.IdentityGroupExport",
        queued=("queued",), processing=("generating",), failed="failed",
        finished_field=None,
    ),
)


def processing_stale_age():
    return timedelta(seconds=settings.JOB_PROCESSING_STALE_SECONDS)


def queued_stale_age():
    return timedelta(hours=settings.JOB_QUEUED_STALE_HOURS)


def _stale_filters(kind, now):
    """Two (statuses, cutoff) pairs describing stale queued and processing rows."""
    return (
        (kind.queued, now - queued_stale_age()),
        (kind.processing, now - processing_stale_age()),
    )


def reap_stuck_jobs(now=None, dry_run=False):
    """Fail every job whose worker is gone; returns counts per job kind.

    Each update re-applies the staleness filter in one statement, so a job that
    finishes (or is touched) while this runs is left alone.
    """
    from uploads.progress import sync_image_progress

    now = now or timezone.now()
    summary = {}
    for kind in JOB_KINDS:
        model = kind.model_class()
        reaped = 0
        image_ids = set()
        for statuses, cutoff in _stale_filters(kind, now):
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

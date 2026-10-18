"""Feature 32: platform-wide analytics for administrators.

Every number here is aggregated from data the platform already stores
(uploads, quality decisions, enhancement results); nothing new is measured or
recorded. Numbers are always exact counts over the requested window, not
sampled estimates.
"""
import shutil
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from camera.models import CameraSession, CameraSnapshot
from downloads.models import DownloadPackage
from enhancement.models import EnhancementStatus, ImageEnhancementResult
from quality_control.models import ImageQualityDecision
from uploads.models import ImageUpload, UploadBatch

User = get_user_model()

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
TOP_REASONS_LIMIT = 10
TOP_MODELS_LIMIT = 10


class InvalidAnalyticsFilter(ValueError):
    """Raised for a filter value the caller must fix; carries a user-facing message."""


def _parse_date(value, *, end_of_day=False):
    """Parse an ISO-8601 date or datetime.

    A bare date (no time component, e.g. "2026-09-22") is a whole-day value:
    for ``date_from`` that means its midnight, for ``date_to`` it means the
    last microsecond of that day, so "give me through 2026-09-22" actually
    includes everything recorded on the 22nd instead of stopping at its
    midnight. A value that already carries a time is used exactly as given.
    """
    if not value:
        return None
    is_bare_date = len(value) == 10  # "YYYY-MM-DD", no time component
    parsed = timezone.datetime.fromisoformat(value)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    if is_bare_date and end_of_day:
        parsed += timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def resolve_window(date_from=None, date_to=None):
    """Validate and default the (from, to) window; raises InvalidAnalyticsFilter."""
    now = timezone.now()
    try:
        parsed_to = _parse_date(date_to, end_of_day=True) or now
        parsed_from = _parse_date(date_from) or (parsed_to - timedelta(days=DEFAULT_WINDOW_DAYS))
    except ValueError as error:
        raise InvalidAnalyticsFilter("date_from and date_to must be ISO-8601 dates.") from error
    if parsed_from > parsed_to:
        raise InvalidAnalyticsFilter("date_from must not be after date_to.")
    if parsed_to - parsed_from > timedelta(days=MAX_WINDOW_DAYS):
        raise InvalidAnalyticsFilter(f"The date range cannot exceed {MAX_WINDOW_DAYS} days.")
    return parsed_from, parsed_to


def _daily_series(queryset, date_field, window_from, window_to):
    """``[{"date": "YYYY-MM-DD", "count": N}, ...]``, one row per day, zero-filled."""
    rows = (
        queryset.filter(**{f"{date_field}__gte": window_from, f"{date_field}__lte": window_to})
        .order_by()      # a model's default ordering would otherwise join the GROUP BY of a distinct() queryset and split each day
        .annotate(day=TruncDate(date_field))
        .values("day")
        .annotate(count=Count("id"))
    )
    by_day = {row["day"]: row["count"] for row in rows}
    series = []
    cursor = window_from.date()
    end = window_to.date()
    while cursor <= end:
        series.append({"date": cursor.isoformat(), "count": by_day.get(cursor, 0)})
        cursor += timedelta(days=1)
    return series


def _top_counts(counter, limit):
    return [{"value": value, "count": count} for value, count in counter.most_common(limit)]


def _model_usage(enhancement_results, limit=TOP_MODELS_LIMIT):
    """Count each restoration/model contributor named in ``algorithm`` strings.

    ``applied_parameters["algorithm"]`` is a compound string such as
    "zero_dce_hybrid_v1+private_gfpgan_v1_4+realesrgan_x4plus"; each
    "+"-joined component is counted separately so a photo that used three
    models contributes to all three counts, not one combined label.
    """
    counter = Counter()
    values = enhancement_results.exclude(applied_parameters__algorithm="").values_list(
        "applied_parameters__algorithm", flat=True
    )
    for algorithm in values:
        if not algorithm:
            continue
        counter.update(algorithm.split("+"))
    return _top_counts(counter, limit)


def _rejection_reasons(quality_decisions, limit=TOP_REASONS_LIMIT):
    """Count each rejection reason recorded on rejected/needs-review decisions."""
    counter = Counter()
    reason_lists = quality_decisions.exclude(
        decision=ImageQualityDecision.Decision.ACCEPTED,
    ).values_list("rejection_reasons", flat=True)
    for reasons in reason_lists:
        counter.update(reasons or [])
    return _top_counts(counter, limit)


UPLOAD_TYPES = ("single", "multiple", "zip")
RECENT_WINDOWS = (("today", 1), ("this_week", 7), ("this_month", 30))


def _percent(part, whole):
    return round(part / whole * 100, 2) if whole else None


def _change_percent(current, previous):
    """Percentage change against the previous period; null when there is nothing to compare with."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _rejected_images(images):
    """Photos that did not pass: refused at upload, or rejected by the quality decision."""
    return images.filter(Q(status="rejected") | Q(quality_decision__decision=ImageQualityDecision.Decision.REJECTED)).distinct()


def _uploads_by_type_per_day(images, window_from, window_to):
    """``[{"date", "single", "multiple", "zip"}]``: photos received per day by how they arrived, zero-filled."""
    rows = (
        images.filter(created_at__gte=window_from, created_at__lte=window_to)
        .annotate(day=TruncDate("created_at"))
        .values("day", "batch__upload_type")
        .annotate(count=Count("id"))
    )
    by_day = {}
    for row in rows:
        by_day.setdefault(row["day"], {})[row["batch__upload_type"]] = row["count"]
    series, cursor = [], window_from.date()
    while cursor <= window_to.date():
        counts = by_day.get(cursor, {})
        series.append({"date": cursor.isoformat(), **{kind: counts.get(kind, 0) for kind in UPLOAD_TYPES}})
        cursor += timedelta(days=1)
    return series


def _enhancement_success_per_week(enhancements, window_from, window_to):
    """Completed and failed shares of the enhancements that finished, per ISO week (``null`` shares for a week with none)."""
    weeks = {}
    for created_at, result_status in enhancements.filter(
        status__in=[EnhancementStatus.COMPLETED, EnhancementStatus.FAILED],
        created_at__gte=window_from, created_at__lte=window_to,
    ).values_list("created_at", "status"):
        year, week, _ = created_at.isocalendar()
        weeks.setdefault((year, week), Counter())[result_status] += 1
    series = []
    for (year, week), counts in sorted(weeks.items()):
        done = counts[EnhancementStatus.COMPLETED]
        failed = counts[EnhancementStatus.FAILED]
        series.append({
            "week": f"{year}-W{week:02d}", "completed": done, "failed": failed,
            "success_percent": _percent(done, done + failed), "failed_percent": _percent(failed, done + failed),
        })
    return series


def _storage_capacity_bytes():
    """The size of the disk the files live on (filesystem storage); ``None`` on object storage, which has no fixed capacity."""
    if getattr(settings, "STORAGE_BACKEND", "filesystem") != "filesystem":
        return None
    try:
        return shutil.disk_usage(settings.MEDIA_ROOT).total
    except OSError:
        return None


def _storage_growth_by_month(owner_id):
    """Cumulative stored bytes (originals plus enhanced outputs) at the end of each month that has uploads."""
    images = ImageUpload.objects.all()
    enhancements = ImageEnhancementResult.objects.all()
    if owner_id is not None:
        images, enhancements = images.filter(owner_id=owner_id), enhancements.filter(owner_id=owner_id)
    added = Counter()
    for model_rows, field in ((images, "file_size"), (enhancements, "output_file_size")):
        for row in model_rows.annotate(month=TruncMonth("created_at")).values("month").annotate(total=Sum(field)).order_by("month"):
            if row["month"]:
                added[row["month"].strftime("%Y-%m")] += row["total"] or 0
    running, series = 0, []
    for month in sorted(added):
        running += added[month]
        series.append({"month": month, "added_bytes": added[month], "total_bytes": running})
    return series


def build_platform_overview(*, window_from, window_to, owner_id=None):
    """The numbers the administrator's Overview, Statistics, Upload Statistics and Rejections screens show beyond the core payload.

    Everything is counted from stored records, in the same window and for the same owner filter as the rest of the analytics. "Rejected"
    here means a photograph refused at upload or rejected by its quality decision (counted once). Percent changes compare the window
    with the equally long window just before it, and are ``null`` when that earlier window is empty.
    """
    window = Q(created_at__gte=window_from, created_at__lte=window_to)
    span = window_to - window_from
    previous = Q(created_at__gte=window_from - span, created_at__lt=window_from)
    images_all = ImageUpload.objects.all()
    enhancements_all = ImageEnhancementResult.objects.all()
    sessions_all = CameraSession.objects.all()
    if owner_id is not None:
        images_all, enhancements_all = images_all.filter(owner_id=owner_id), enhancements_all.filter(owner_id=owner_id)
        sessions_all = sessions_all.filter(owner_id=owner_id)
    images = images_all.filter(window)
    sessions = sessions_all.filter(started_at__gte=window_from, started_at__lte=window_to)

    uploads_total = images.count()
    rejected = _rejected_images(images).count()
    now = timezone.now()
    recent = {name: images_all.filter(created_at__gte=now - timedelta(days=days)).count() for name, days in RECENT_WINDOWS}
    by_type = {kind: images.filter(batch__upload_type=kind).count() for kind in UPLOAD_TYPES}
    average_size = images.aggregate(avg=Avg("file_size"))["avg"]

    zip_bytes = DownloadPackage.objects.aggregate(total=Sum("file_size"))["total"] or 0
    snapshot_bytes = CameraSnapshot.objects.aggregate(total=Sum("image__file_size"))["total"] or 0
    capacity = _storage_capacity_bytes()
    stored = (images_all.aggregate(total=Sum("file_size"))["total"] or 0) + (enhancements_all.aggregate(total=Sum("output_file_size"))["total"] or 0)

    return {
        "uploads": {
            "total": uploads_total,
            "recent": recent,
            "by_type": by_type,
            "average_file_size_bytes": round(average_size) if average_size else None,
            "per_day_by_type": _uploads_by_type_per_day(images, window_from, window_to),
        },
        "rejections": {
            "total": rejected,
            "rate_percent": _percent(rejected, uploads_total),
            "per_day": _daily_series(_rejected_images(images_all), "created_at", window_from, window_to),
        },
        "enhancement_success_per_week": _enhancement_success_per_week(enhancements_all, window_from, window_to),
        "live_sessions": {
            "total": sessions.count(),
            "per_day": _daily_series(sessions, "started_at", window_from, window_to),
        },
        "storage": {
            "originals_and_outputs_bytes": stored,
            "zip_packages_bytes": zip_bytes,
            "camera_snapshots_bytes": snapshot_bytes,
            "capacity_bytes": capacity,
            "used_percent": _percent(stored + zip_bytes, capacity) if capacity else None,
            "growth_by_month": _storage_growth_by_month(owner_id),
        },
        "change_vs_previous_period": {
            "uploads_percent": _change_percent(uploads_total, images_all.filter(previous).count()),
            "enhancements_percent": _change_percent(
                enhancements_all.filter(window).count(), enhancements_all.filter(previous).count(),
            ),
            "rejections_percent": _change_percent(
                rejected, _rejected_images(images_all.filter(previous)).count(),
            ),
            "live_sessions_percent": _change_percent(
                sessions.count(),
                sessions_all.filter(started_at__gte=window_from - span, started_at__lt=window_from).count(),
            ),
        },
    }


def build_admin_analytics(*, date_from=None, date_to=None, status=None, model=None, owner=None):
    """The full Feature 32 payload. Raises InvalidAnalyticsFilter on a bad filter."""
    window_from, window_to = resolve_window(date_from, date_to)
    if status and status not in EnhancementStatus.values:
        raise InvalidAnalyticsFilter(f"Unknown status '{status}'. Choose one of: {', '.join(EnhancementStatus.values)}.")
    owner_id = None
    if owner not in (None, ""):
        try:
            owner_id = int(owner)
        except (TypeError, ValueError) as error:
            raise InvalidAnalyticsFilter("owner must be a user id.") from error

    window = Q(created_at__gte=window_from, created_at__lte=window_to)

    batches = UploadBatch.objects.filter(window)
    images = ImageUpload.objects.filter(window)
    enhancements = ImageEnhancementResult.objects.filter(window)
    quality_decisions = ImageQualityDecision.objects.filter(window)
    if owner_id is not None:
        batches = batches.filter(owner_id=owner_id)
        images = images.filter(owner_id=owner_id)
        enhancements = enhancements.filter(owner_id=owner_id)
        quality_decisions = quality_decisions.filter(owner_id=owner_id)
    if status:
        enhancements = enhancements.filter(status=status)
    if model:
        enhancements = enhancements.filter(applied_parameters__algorithm__icontains=model)

    enhancement_by_status = dict(
        enhancements.values_list("status").annotate(count=Count("id")).order_by()
    )
    decision_counts = dict(
        quality_decisions.exclude(decision="").values_list("decision").annotate(count=Count("id")).order_by()
    )
    accepted = decision_counts.get(ImageQualityDecision.Decision.ACCEPTED, 0)
    decided = sum(decision_counts.values())
    success_rate = round(accepted / decided * 100, 2) if decided else None

    storage = enhancements.aggregate(bytes=Sum("output_file_size"))["bytes"] or 0
    original_storage = images.aggregate(bytes=Sum("file_size"))["bytes"] or 0
    average_enhancement_ms = enhancements.filter(
        status=EnhancementStatus.COMPLETED,
    ).aggregate(avg=Avg("processing_duration_ms"))["avg"]
    average_quality_ms = quality_decisions.filter(
        status=ImageQualityDecision.Status.COMPLETED,
    ).aggregate(avg=Avg("processing_duration_ms"))["avg"]

    return {
        "range": {"from": window_from.isoformat(), "to": window_to.isoformat()},
        "filters_applied": {
            "status": status or None, "model": model or None, "owner": owner_id,
        },
        "totals": {
            "users": {
                "total": User.objects.count(),
                "verified": User.objects.filter(is_email_verified=True).count(),
                "suspended": User.objects.filter(is_suspended=True).count(),
            },
            "batches": batches.count(),
            "images": {
                "total": images.count(),
                "accepted": images.filter(status="uploaded").count(),
                "rejected": images.filter(status="rejected").count(),
            },
            "enhancements": {
                "total": enhancements.count(),
                "by_status": enhancement_by_status,
            },
            "quality_decisions": {
                "total": quality_decisions.count(),
                "by_decision": decision_counts,
            },
        },
        "success_rate_percent": success_rate,
        "average_processing_time_ms": {
            "enhancement": round(average_enhancement_ms, 1) if average_enhancement_ms else None,
            "quality_analysis": round(average_quality_ms, 1) if average_quality_ms else None,
        },
        "top_rejection_reasons": _rejection_reasons(quality_decisions),
        "model_usage": _model_usage(enhancements),
        "storage_bytes": {
            "originals": original_storage,
            "enhanced_outputs": storage,
            "total": original_storage + storage,
        },
        "charts": {
            "uploads_per_day": _daily_series(images, "created_at", window_from, window_to),
            "enhancements_per_day": _daily_series(enhancements, "created_at", window_from, window_to),
        },
        "overview": build_platform_overview(window_from=window_from, window_to=window_to, owner_id=owner_id),
        "generated_at": timezone.now().isoformat(),
    }

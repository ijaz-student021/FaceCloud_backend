"""Feature 32: platform-wide analytics for administrators.

Every number here is aggregated from data the platform already stores
(uploads, quality decisions, enhancement results); nothing new is measured or
recorded. Numbers are always exact counts over the requested window, not
sampled estimates.
"""
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

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
    "opencv_adaptive_restoration_router_v2+dynamic_vivid_boost+identity_guard";
    each "+"-joined component is counted separately so a photo that used
    several contributes to every count. Results stored before the learned
    stages were retired (2026-10-15) still name them, e.g.
    "zero_dce_hybrid_v1+private_gfpgan_v1_4", and keep being counted.
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
        "generated_at": timezone.now().isoformat(),
    }

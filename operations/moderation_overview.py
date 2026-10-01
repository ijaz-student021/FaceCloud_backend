"""Feature 33: platform-wide moderator monitoring for administrators.

Deferred when Feature 33 first shipped because Feature 27's moderator review
queue did not exist yet, so there was nothing operational to monitor beyond the
role listing `AdminUserListView` already gives. Feature 27 is complete now
(``moderation`` app), so this aggregates its own org-scoped
`moderation.services.dashboard_summary` across every organization, the same way
`operations/analytics.py` aggregates enhancement data platform-wide -- nothing
new is measured or recorded here.
"""
from django.contrib.auth import get_user_model
from django.db.models import Count, Min, Q
from django.utils import timezone

from moderation.models import ModeratorReview
from organizations.models import Organization

User = get_user_model()

DEFAULT_STALE_HOURS = 48
MAX_STALE_HOURS = 24 * 90
TOP_ORGANIZATIONS_LIMIT = 20
TOP_MODERATORS_LIMIT = 20


class InvalidModerationOverviewFilter(ValueError):
    """Raised for a filter value the caller must fix; carries a user-facing message."""


def _parse_stale_hours(value):
    if value in (None, ""):
        return DEFAULT_STALE_HOURS
    try:
        hours = int(value)
    except ValueError as error:
        raise InvalidModerationOverviewFilter("stale_hours must be a whole number.") from error
    if not 1 <= hours <= MAX_STALE_HOURS:
        raise InvalidModerationOverviewFilter(f"stale_hours must be between 1 and {MAX_STALE_HOURS}.")
    return hours


OPEN_STATUSES = (ModeratorReview.Status.PENDING, ModeratorReview.Status.IN_REVIEW, ModeratorReview.Status.ESCALATED)


def build_admin_moderation_overview(*, stale_hours=None):
    """Platform totals, the organizations most needing attention, and moderator workload.

    ``stale_hours`` (default 48) sets how old a still-open review must be to
    count as stale -- the single most actionable number here, since a review
    nobody has looked at in days is exactly what an administrator wants
    surfaced that an organization's own dashboard cannot show them.
    """
    hours = _parse_stale_hours(stale_hours)
    stale_cutoff = timezone.now() - timezone.timedelta(hours=hours)
    reviews = ModeratorReview.objects.all()
    today = timezone.localdate()

    totals = {
        "pending": reviews.filter(status=ModeratorReview.Status.PENDING).count(),
        "in_review": reviews.filter(status=ModeratorReview.Status.IN_REVIEW).count(),
        "escalated": reviews.filter(status=ModeratorReview.Status.ESCALATED).count(),
        "completed_today": reviews.filter(decided_at__date=today).count(),
        "approved_total": reviews.filter(status=ModeratorReview.Status.APPROVED).count(),
        "rejected_total": reviews.filter(status=ModeratorReview.Status.REJECTED).count(),
        "corrected_total": reviews.filter(status=ModeratorReview.Status.CORRECTED).count(),
        "stale_open": reviews.filter(status__in=OPEN_STATUSES, created_at__lt=stale_cutoff).count(),
    }

    organizations = (
        Organization.objects.annotate(
            pending=Count("moderator_reviews", filter=Q(moderator_reviews__status=ModeratorReview.Status.PENDING)),
            in_review=Count("moderator_reviews", filter=Q(moderator_reviews__status=ModeratorReview.Status.IN_REVIEW)),
            escalated=Count("moderator_reviews", filter=Q(moderator_reviews__status=ModeratorReview.Status.ESCALATED)),
            stale_open=Count(
                "moderator_reviews",
                filter=Q(moderator_reviews__status__in=OPEN_STATUSES, moderator_reviews__created_at__lt=stale_cutoff),
            ),
            oldest_open_at=Min("moderator_reviews__created_at", filter=Q(moderator_reviews__status__in=OPEN_STATUSES)),
        )
        .filter(Q(pending__gt=0) | Q(in_review__gt=0) | Q(escalated__gt=0))
        .order_by("-escalated", "-stale_open", "-pending")[:TOP_ORGANIZATIONS_LIMIT]
    )
    organization_rows = [
        {
            "organization_id": org.pk, "organization_name": org.name,
            "pending": org.pending, "in_review": org.in_review, "escalated": org.escalated,
            "stale_open": org.stale_open, "oldest_open_at": org.oldest_open_at,
        }
        for org in organizations
    ]

    # Two separate grouped queries, not one queryset annotated with both counts:
    # ``decided_by`` has no reverse accessor (``related_name="+"``) to annotate
    # through, and annotating two independent Count()s over different FK paths
    # on the same base queryset risks a join fan-out multiplying one count by
    # the other's row count -- a classic Django ORM trap this avoids entirely.
    assigned_counts = dict(
        reviews.filter(status__in=OPEN_STATUSES, assigned_moderator__isnull=False)
        .values("assigned_moderator_id")
        .annotate(count=Count("id"))
        .values_list("assigned_moderator_id", "count")
    )
    decided_counts = dict(
        reviews.filter(decided_by__isnull=False)
        .values("decided_by_id")
        .annotate(count=Count("id"))
        .values_list("decided_by_id", "count")
    )
    moderator_ids = set(assigned_counts) | set(decided_counts)
    usernames = dict(
        get_moderator_model().objects.filter(pk__in=moderator_ids).values_list("pk", "username")
    )
    moderator_rows = sorted(
        (
            {
                "moderator_id": moderator_id, "username": usernames.get(moderator_id, ""),
                "assigned_open": assigned_counts.get(moderator_id, 0),
                "decided_total": decided_counts.get(moderator_id, 0),
            }
            for moderator_id in moderator_ids
        ),
        key=lambda row: (-row["assigned_open"], -row["decided_total"]),
    )[:TOP_MODERATORS_LIMIT]

    return {
        "stale_hours": hours,
        "totals": totals,
        "organizations_needing_attention": organization_rows,
        "moderator_workload": moderator_rows,
    }


def get_moderator_model():
    from django.contrib.auth import get_user_model

    return get_user_model()

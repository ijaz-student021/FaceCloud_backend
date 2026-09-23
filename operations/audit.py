"""Feature 33: a single, platform-wide audit feed for administrators.

Combines the two event trails the platform already keeps -- ``AuthEvent``
(accounts: registration, login, password, role changes) and ``ActivityEvent``
(everything else: uploads, processing, downloads, groups, shares, privacy) --
into one chronological, filterable list. Neither table is changed; this only
reads them.

Both tables are queried and merge-sorted in Python rather than unioned in SQL,
because they have different columns and vocabularies. That is appropriate for
an admin's "what happened recently / around this user" view, not for scanning
a full history: each request reads at most
``page_size`` from each table.
"""
from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.models import AuthEvent
from activity.models import ActivityEvent

User = get_user_model()

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
SOURCES = ("auth", "activity")


class InvalidAuditFilter(ValueError):
    """Raised for a filter value the caller must fix; carries a user-facing message."""


def _auth_row(event):
    return {
        "source": "auth",
        "id": event.pk,
        "user_id": event.user_id,
        "category": "auth",
        "event": event.event,
        "identifier": event.identifier,
        "object_type": "",
        "object_id": "",
        "details": event.details,
        "created_at": event.created_at,
    }


def _activity_row(event):
    return {
        "source": "activity",
        "id": event.pk,
        "user_id": event.user_id,
        "category": event.category,
        "event": event.event,
        "identifier": "",
        "object_type": event.object_type,
        "object_id": event.object_id,
        "details": event.details,
        "created_at": event.created_at,
    }


def build_audit_feed(*, user=None, category=None, source=None, page=1, page_size=DEFAULT_PAGE_SIZE):
    """A paginated, chronological page of admin-audit rows; raises InvalidAuditFilter."""
    if source is not None and source not in SOURCES:
        raise InvalidAuditFilter(f"Unknown source '{source}'. Choose one of: {', '.join(SOURCES)}.")
    requested_category = category
    if category == "auth":
        # AuthEvent has no category of its own; "auth" here means "auth events
        # only", i.e. it also pins the source, rather than being a real
        # ActivityEvent.Category value to filter that table by.
        if source not in (None, "auth"):
            raise InvalidAuditFilter("category='auth' is only compatible with source='auth'.")
        source = "auth"
        category = None
    try:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    except (TypeError, ValueError) as error:
        raise InvalidAuditFilter("page and page_size must be integers.") from error
    user_id = None
    if user not in (None, ""):
        try:
            user_id = int(user)
        except (TypeError, ValueError) as error:
            raise InvalidAuditFilter("user must be a user id.") from error

    # Each source's own candidate window only needs to reach the end of the
    # requested page, since rows are merged newest-first afterward.
    fetch = page * page_size

    auth_rows, activity_rows = [], []
    if source in (None, "auth"):
        auth_events = AuthEvent.objects.all()
        if user_id is not None:
            auth_events = auth_events.filter(user_id=user_id)
        if category not in (None, "", "auth"):
            auth_events = auth_events.none()  # AuthEvent has no other category
        auth_rows = [_auth_row(event) for event in auth_events.order_by("-created_at")[:fetch]]

    if source in (None, "activity"):
        activity_events = ActivityEvent.objects.all()
        if user_id is not None:
            activity_events = activity_events.filter(user_id=user_id)
        if category:
            if category not in ActivityEvent.Category.values:
                raise InvalidAuditFilter(
                    f"Unknown category '{category}'. Choose one of: "
                    f"auth, {', '.join(ActivityEvent.Category.values)}."
                )
            activity_events = activity_events.filter(category=category)
        activity_rows = [_activity_row(event) for event in activity_events.order_by("-created_at")[:fetch]]

    merged = sorted(auth_rows + activity_rows, key=lambda row: row["created_at"], reverse=True)
    start = (page - 1) * page_size
    page_rows = merged[start:start + page_size]
    has_next = len(merged) > start + page_size or len(auth_rows) == fetch or len(activity_rows) == fetch

    return {
        "generated_at": timezone.now(),
        "filters_applied": {"user_id": user_id, "category": requested_category or None, "source": source or None},
        "page": page,
        "page_size": page_size,
        "has_next": has_next,
        "results": page_rows,
    }

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
from activity.presentation import describe

User = get_user_model()

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
SOURCES = ("auth", "activity")


class InvalidAuditFilter(ValueError):
    """Raised for a filter value the caller must fix; carries a user-facing message."""


# The timeline's sentences are written to the person they happened to ("Your password was changed."); an administrator reads about someone else.
ADMIN_WORDING = {
    "registered": "Account created.",
    "email_verified": "Email address verified.",
    "login": "Signed in.",
    "login_failed": "A sign-in attempt failed.",
    "login_locked": "Sign-in was temporarily locked after repeated failures.",
    "logout": "Signed out.",
    "logout_all": "Signed out of every device.",
    "password_changed": "Password changed.",
    "password_reset": "Password reset.",
    "profile_updated": "Profile updated.",
    "role_changed": "Role changed by an administrator.",
    "suspended": "Account suspended.",
    "reactivated": "Account reactivated.",
    "account_deleted": "Account deleted.",
    "admin_created_account": "Account created by an administrator.",
    "consent_granted": "Face-recognition consent given.",
    "consent_withdrawn": "Face-recognition consent withdrawn.",
    "facial_data_erased": "Facial data erased.",
    "data_exported": "Personal data export created.",
}
CATEGORY_LABELS = {"auth": "Accounts", **{value: label for value, label in ActivityEvent.Category.choices}}


def decorate_rows(rows):
    """Add what the audit screen shows to each row, without changing or removing any existing key.

    ``actor`` (``null`` when the account no longer exists and no identifier was recorded), ``actor_name``, ``actor_role``, ``type`` and
    ``status`` (read the same way as the user timeline), ``message`` (a sentence about what happened, in the third person), ``resource``
    (what it happened to) and ``ip_address`` (only sign-in events record one; ``null`` for the rest).
    """
    users = {user.pk: user for user in User.objects.filter(pk__in={row["user_id"] for row in rows if row["user_id"]})}
    for row in rows:
        user = users.get(row["user_id"])
        kind, event_status, message = describe(row["category"], row["event"])
        row["actor"] = {"id": user.pk, "username": user.username, "role": user.role} if user else None
        row["actor_name"] = user.username if user else (row["identifier"] or None)
        row["actor_role"] = user.role if user else None
        row["type"] = kind
        row["status"] = event_status
        row["message"] = ADMIN_WORDING.get(row["event"], message)
        row["resource"] = (
            f"{row['object_type']} {row['object_id']}".strip() if row["object_type"] else CATEGORY_LABELS.get(row["category"], row["category"])
        )
        row.setdefault("ip_address", None)
    return rows


def _auth_row(event):
    return {
        "source": "auth",
        "id": event.pk,
        "user_id": event.user_id,
        "category": "auth",
        "event": event.event,
        "identifier": event.identifier,
        "ip_address": event.ip_address,
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
    page_rows = decorate_rows(merged[start:start + page_size])
    has_next = len(merged) > start + page_size or len(auth_rows) == fetch or len(activity_rows) == fetch

    return {
        "generated_at": timezone.now(),
        "filters_applied": {"user_id": user_id, "category": requested_category or None, "source": source or None},
        "page": page,
        "page_size": page_size,
        "has_next": has_next,
        "results": page_rows,
    }

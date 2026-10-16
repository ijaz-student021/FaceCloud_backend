from itertools import islice

from .models import ActivityEvent
from .presentation import describe


def record_activity_event(
    user, category, event, object_type="", object_id="", details=None
):
    """Record one entry in a user's activity timeline.

    Safe to call from any app's services/tasks: this app has no dependency
    on the callers, so it never introduces a circular import.
    """
    return ActivityEvent.objects.create(
        user=user,
        category=category,
        event=event,
        object_type=object_type,
        object_id=str(object_id) if object_id != "" else "",
        details=details or {},
    )


def _presentation(category, event_name, created_at):
    kind, event_status, message = describe(category, event_name)
    return {"type": kind, "status": event_status, "message": message, "timestamp": created_at}


def _normalize_activity_event(event):
    return {
        "id": f"activity-{event.pk}",
        **_presentation(event.category, event.event, event.created_at),
        "source": "activity",
        "category": event.category,
        "event": event.event,
        "object_type": event.object_type,
        "object_id": event.object_id,
        "identifier": "",
        "ip_address": None,
        "details": event.details,
        "created_at": event.created_at,
    }


def _normalize_auth_event(event):
    return {
        "id": f"auth-{event.pk}",
        **_presentation("auth", event.event, event.created_at),
        "source": "auth",
        "category": "auth",
        "event": event.event,
        "object_type": "",
        "object_id": "",
        "identifier": event.identifier,
        "ip_address": event.ip_address,
        "details": event.details,
        "created_at": event.created_at,
    }


def _merge_sorted(*sorted_iterables):
    """Merge iterables already sorted descending by created_at."""
    iterators = [iter(it) for it in sorted_iterables]
    heads = []
    for index, iterator in enumerate(iterators):
        try:
            heads.append((next(iterator), index))
        except StopIteration:
            heads.append((None, index))

    while any(head is not None for head, _ in heads):
        best_index = None
        best_value = None
        for head, index in heads:
            if head is None:
                continue
            if best_value is None or head["created_at"] > best_value["created_at"]:
                best_value = head
                best_index = index
        yield best_value
        try:
            heads[best_index] = (next(iterators[best_index]), best_index)
        except StopIteration:
            heads[best_index] = (None, best_index)


def get_user_timeline(user, category=None, page=1, page_size=20, fetch_cap=500):
    """Return a merged, paginated activity timeline for one user.

    Combines ActivityEvent (uploads, processing, moderation, downloads,
    identity groups, shares, admin actions) with the pre-existing AuthEvent
    trail so the frontend has a single feed to render.
    """
    from accounts.models import AuthEvent

    page = max(int(page), 1)
    page_size = max(min(int(page_size), 100), 1)
    # Fetch one extra record beyond the current page so `has_next` reflects
    # reality. This merge-sorts two querysets in Python rather than a SQL
    # UNION, so very deep pagination (past `fetch_cap` combined events) is
    # a known simplification appropriate for this project's event volume.
    needed = page * page_size + 1

    activity_qs = ActivityEvent.objects.filter(user=user)
    if category and category != "auth":
        activity_qs = activity_qs.filter(category=category)
    activity_events = (
        []
        if category == "auth"
        else [
            _normalize_activity_event(event)
            for event in islice(activity_qs.iterator(), min(needed, fetch_cap))
        ]
    )

    auth_events = (
        []
        if category not in (None, "auth")
        else [
            _normalize_auth_event(event)
            for event in islice(
                AuthEvent.objects.filter(user=user).iterator(),
                min(needed, fetch_cap),
            )
        ]
    )

    merged = list(islice(_merge_sorted(activity_events, auth_events), needed))
    start = (page - 1) * page_size
    page_items = merged[start:start + page_size]
    has_next = len(merged) > start + page_size
    return {
        "page": page,
        "page_size": page_size,
        "has_next": has_next,
        "results": page_items,
    }

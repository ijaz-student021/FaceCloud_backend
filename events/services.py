"""Automatic event/timeline grouping (Feature 48).

Events are computed on demand from upload timestamps, so they can never drift
out of date after a photo is deleted or uploaded. Only ``created_at`` is read:
no face, embedding or identity data is touched (Feature 41: not facial data).
An event's id is its first photo's id, which stays put when later photos are
appended to it.
"""
from datetime import timedelta

from django.conf import settings

from uploads.models import ImageUpload

MIN_GAP_HOURS = 0.25
MAX_GAP_HOURS = 168.0


def default_gap_hours():
    return float(settings.EVENT_GAP_HOURS)


def uploaded_images(owner):
    return ImageUpload.objects.filter(owner=owner, status="uploaded").exclude(original_file="")


def compute_events(owner, gap_hours=None):
    """Return the owner's events, newest first.

    A new event starts whenever the time since the previous photo exceeds
    ``gap_hours``. Each event is a dict with ``id`` (first photo's id),
    ``start``/``end`` datetimes, ``photo_count`` and ``image_ids`` (oldest first).
    """
    gap = timedelta(hours=default_gap_hours() if gap_hours is None else gap_hours)
    rows = uploaded_images(owner).order_by("created_at", "id").values_list("id", "created_at")
    events = []
    current = None
    for image_id, created_at in rows:
        if current is not None and created_at - current["end"] <= gap:
            current["end"] = created_at
            current["image_ids"].append(image_id)
        else:
            current = {"id": image_id, "start": created_at, "end": created_at, "image_ids": [image_id]}
            events.append(current)
    for event in events:
        event["photo_count"] = len(event["image_ids"])
    events.reverse()
    return events


def get_event(owner, event_id, gap_hours=None):
    for event in compute_events(owner, gap_hours):
        if event["id"] == event_id:
            return event
    return None

"""Memories: a few of the best photographs of a moment, chosen for the person: "2 years ago today", "Moments from 12 Oct", "With Ayesha".

Nothing is stored. A memory is worked out from the person's own photographs when it is asked for, so it is always current, never shows a deleted or rejected photograph, and has no
table to keep in step. Its id says how to rebuild it (``otd:<year>``, ``event:<first photograph id>``, ``person:<folder id>:<YYYY-MM>``) and is stable for the same photographs.

A photograph is dated by when it was taken (``taken_at``, from EXIF), else when it was uploaded (``ImageUpload.captured_at``).

Choosing the best photographs of a moment is deterministic: favourites first, then the higher quality score, then one with a face, then the earlier one; a photograph that duplicates
one already chosen, or was taken within a minute of one, is skipped, so a burst of ten shots gives one.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from duplicates.models import DuplicateMatch
from enhancement.eligibility import not_rejected
from identities.avatars import CONFIRMED
from identities.models import IdentityGroup, IdentityMembership
from identities.people import is_named, people_queryset
from uploads.models import ImageUpload

LOOKBACK_YEARS = 30
MIN_ON_THIS_DAY = 3
MIN_EVENT = 5
MIN_PERSON = 10
EVENT_GAP = timedelta(hours=6)
RECENT_DAYS = 180
PROXIMITY = timedelta(seconds=60)
CARD_HIGHLIGHTS = 12
DETAIL_HIGHLIGHTS = 24
MAX_PEOPLE_MEMORIES = 3
MAX_EVENT_MEMORIES = 6
MAX_ON_THIS_DAY = 3


@dataclass
class Memory:
    id: str
    kind: str
    title: str
    subtitle: str
    rows: list = field(repr=False)             # every photograph of the moment: dicts from ``photo_rows``, oldest first
    highlights: list = field(repr=False, default_factory=list)
    filter: dict = field(default_factory=dict)  # the My Images query that lists all of the moment's photographs

    @property
    def start(self):
        return self.rows[0]["captured"] if self.rows else None

    @property
    def end(self):
        return self.rows[-1]["captured"] if self.rows else None


def photo_rows(user, *, since=None, until=None, day=None, ids=None):
    """The person's photographs as light dicts, oldest first, without those the checks rejected or that are not stored photographs."""
    queryset = not_rejected(ImageUpload.objects.filter(owner=user, status="uploaded")).annotate(captured=Coalesce("taken_at", "created_at"))
    if since is not None:
        queryset = queryset.filter(captured__gte=since)
    if until is not None:
        queryset = queryset.filter(captured__lt=until)
    if day is not None:
        queryset = queryset.filter(captured__month=day.month, captured__day=day.day)
    if ids is not None:
        queryset = queryset.filter(pk__in=list(ids))
    return list(queryset.order_by("captured", "pk").values(
        "id", "captured", "is_favorite", score=F("quality_decision__overall_score"), faces=F("face_validation__face_count"),
    ))


def _duplicates_among(user, ids):
    """``{id: set of ids it duplicates}`` among ``ids`` (a match a person said is not a duplicate does not count)."""
    pairs = DuplicateMatch.objects.filter(owner=user, source_image_id__in=ids, matched_image_id__in=ids).exclude(review_status="not_duplicate").values_list("source_image_id", "matched_image_id")
    linked = {}
    for source, candidate in pairs:
        linked.setdefault(source, set()).add(candidate)
        linked.setdefault(candidate, set()).add(source)
    return linked


def select_highlights(user, rows, limit):
    """The best ``limit`` photographs of ``rows``, best first, without duplicates or shots within a minute of one already chosen."""
    if not rows:
        return []
    linked = _duplicates_among(user, [row["id"] for row in rows])
    ranked = sorted(rows, key=lambda row: (not row["is_favorite"], -(row["score"] or 0), not (row["faces"] or 0), row["captured"], row["id"]))
    chosen = []
    for row in ranked:
        if len(chosen) >= limit:
            break
        if any(other["id"] in linked.get(row["id"], ()) or abs(other["captured"] - row["captured"]) < PROXIMITY for other in chosen):
            continue
        chosen.append(row)
    return chosen


def _day_title(start, end):
    if start.date() == end.date():
        return f"{start.day} {start:%b}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {start:%b}"
    return f"{start.day} {start:%b} – {end.day} {end:%b}"


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def on_this_day(user, today=None):
    today = today or timezone.localdate()
    rows = [row for row in photo_rows(user, day=today) if today.year - LOOKBACK_YEARS <= row["captured"].year < today.year]
    by_year = {}
    for row in rows:
        by_year.setdefault(row["captured"].year, []).append(row)
    memories = []
    for year in sorted(by_year, reverse=True):
        group = by_year[year]
        if len(group) < MIN_ON_THIS_DAY:
            continue
        ago = today.year - year
        memories.append(Memory(
            id=f"otd:{year}", kind="on_this_day", title=f"{_plural(ago, 'year')} ago today", subtitle=f"{_plural(len(group), 'photo')} from {group[0]['captured']:%-d %b %Y}",
            rows=group, filter={"taken_from": date(year, today.month, today.day).isoformat(), "taken_to": date(year, today.month, today.day).isoformat()},
        ))
    return memories[:MAX_ON_THIS_DAY]


def cluster(rows, gap=EVENT_GAP):
    """Split ``rows`` (oldest first) into runs where neighbours are less than ``gap`` apart."""
    runs, current = [], []
    for row in rows:
        if current and row["captured"] - current[-1]["captured"] > gap:
            runs.append(current)
            current = []
        current.append(row)
    if current:
        runs.append(current)
    return runs


def _event_memory(group):
    start, end = group[0]["captured"], group[-1]["captured"]
    return Memory(
        id=f"event:{group[0]['id']}", kind="event", title=f"Moments from {_day_title(start, end)}", subtitle=_plural(len(group), "photo"), rows=group,
        filter={"taken_from": start.date().isoformat(), "taken_to": end.date().isoformat()},
    )


def recent_events(user, now=None):
    now = now or timezone.now()
    runs = cluster(photo_rows(user, since=now - timedelta(days=RECENT_DAYS)))
    events = [_event_memory(group) for group in runs if len(group) >= MIN_EVENT]
    return list(reversed(events))[:MAX_EVENT_MEMORIES]


def person_memory(user, group, today=None):
    today = today or timezone.localdate()
    ids = list(IdentityMembership.objects.filter(identity_group=group, state__in=CONFIRMED).values_list("identity_face__image_id", flat=True).distinct())
    rows = photo_rows(user, ids=ids)
    if len(rows) < MIN_PERSON:
        return None
    return Memory(id=f"person:{group.pk}:{today:%Y-%m}", kind="person", title=f"With {group.display_name}", subtitle=_plural(len(rows), "photo"), rows=rows, filter={"person": group.pk})


def people_memories(user, today=None):
    named = [group for group in people_queryset(user)[:20] if is_named(group)]
    memories = [memory for memory in (person_memory(user, group, today) for group in named[:10]) if memory is not None]
    return memories[:MAX_PEOPLE_MEMORIES]


def build_memories(user, limit=10, now=None):
    """The person's memories, "on this day" first, then recent events (newest first), then people; each with its highlights chosen."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    memories = [*on_this_day(user, today), *recent_events(user, now), *people_memories(user, today)][:limit]
    for memory in memories:
        memory.highlights = select_highlights(user, memory.rows, CARD_HIGHLIGHTS)
    return memories


def memory_by_id(user, memory_id, now=None):
    """Rebuild one memory from its id, or ``None`` when it no longer exists (a stale id)."""
    now = now or timezone.now()
    kind, _, rest = memory_id.partition(":")
    memory = None
    try:
        if kind == "otd":
            year = int(rest)
            today = timezone.localtime(now).date()
            memory = next((m for m in on_this_day(user, today) if m.id == memory_id), None)
            if memory is None and today.year - LOOKBACK_YEARS <= year < today.year:
                group = [row for row in photo_rows(user, day=today) if row["captured"].year == year]
                if len(group) >= MIN_ON_THIS_DAY:
                    memory = next((m for m in on_this_day_for_year(group, today, year)), None)
        elif kind == "event":
            first = int(rest)
            anchor = ImageUpload.objects.filter(owner=user, pk=first, status="uploaded").annotate(captured=Coalesce("taken_at", "created_at")).values_list("captured", flat=True).first()
            if anchor is not None:
                neighbours = photo_rows(user, since=anchor - timedelta(days=7), until=anchor + timedelta(days=7))
                for group in cluster(neighbours):
                    if group[0]["id"] == first and len(group) >= MIN_EVENT:
                        memory = _event_memory(group)
        elif kind == "person":
            group_id = int(rest.split(":")[0])
            group = IdentityGroup.objects.filter(owner=user, pk=group_id, state=IdentityGroup.State.ACTIVE).first()
            if group is not None and is_named(group):
                memory = person_memory(user, group, timezone.localtime(now).date())
                if memory is not None:
                    memory.id = memory_id
    except (ValueError, TypeError, OverflowError):
        return None
    if memory is not None:
        memory.highlights = select_highlights(user, memory.rows, DETAIL_HIGHLIGHTS)
    return memory


def on_this_day_for_year(group, today, year):
    """The memory of one earlier year, built from its rows, for an id that is no longer among the few the list shows."""
    return [Memory(
        id=f"otd:{year}", kind="on_this_day", title=f"{_plural(today.year - year, 'year')} ago today", subtitle=f"{_plural(len(group), 'photo')} from {group[0]['captured']:%-d %b %Y}",
        rows=group, filter={"taken_from": date(year, today.month, today.day).isoformat(), "taken_to": date(year, today.month, today.day).isoformat()},
    )]


def parse_date(value):
    """A ``YYYY-MM-DD`` query value as a date, or ``None``."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

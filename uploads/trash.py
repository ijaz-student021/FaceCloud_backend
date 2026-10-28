"""The trash: delete with a way back, for ``settings.TRASH_RETENTION_DAYS`` days.

Moving a photograph to the trash sets ``ImageUpload.trashed_at``. Because the default manager (``ImageUpload.objects``) hides such rows, the photograph is gone from
My Images, search, albums, person folders, events, memories, the editor and everything else that lists photographs without those places doing anything. What the
trash does itself:

* **Person folders.** The photograph's memberships are taken out the way a rejected one is (``identities.services.remove_photo_from_folders``: state ``removed``,
  the folder recounted, an auto-named folder left empty archived), and what was there is written to a ``TrashEntry`` so ``restore`` can put it back exactly.
* **Ready ZIPs and folder exports** that hold a copy are expired, as a permanent delete does.
* **Albums, favourite flag, checks' results, enhanced copies and files** are left alone: they come back with the photograph, and the orphan-file sweep keeps the
  files of a trashed photograph (``privacy.cleanup.referenced_media_names`` reads the unfiltered rows).

A photograph that is still being processed (queued checks, an enhancement waiting or running) or that waits for a moderator is **not** moved: the answer says so,
and the person can try again in a moment. Purging a photograph is the existing permanent delete (``uploads.deletion.delete_images``), run by the daily task
``uploads.purge_trash`` for photographs older than the retention, or at once by the person.
"""
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .deletion import delete_images, expire_ready_copies
from .models import ImageUpload, TrashEntry

logger = logging.getLogger(__name__)

STILL_PROCESSING = "still_processing"
IN_REVIEW = "in_review"
PURGE_BATCH_SIZE = 200


def retention():
    return timedelta(days=settings.TRASH_RETENTION_DAYS)


def purge_at(image):
    """When the trash will delete ``image`` for good (``None`` if it is not in the trash)."""
    return image.trashed_at + retention() if image.trashed_at else None


def days_left(image, now=None):
    """Whole days until ``image`` is purged, rounded up so a photograph trashed a minute ago shows the full retention (``None`` if it is not in the trash)."""
    deadline = purge_at(image)
    if deadline is None:
        return None
    return max(0, math.ceil((deadline - (now or timezone.now())).total_seconds() / 86400))


def blocked_reason(image):
    """Why ``image`` cannot be moved to the trash right now, or ``None``: it is still being processed, or it waits for a moderator.

    "Still being processed" is what the progress screen would say (``uploads.progress._state_for_image``, worked out from the real results, never from a stored
    status that may lag): the checks are queued or running, an enhancement is waiting for a slot, queued or running.
    """
    from enhancement.auto import ACTIVE_STATUSES
    from enhancement.models import ImageEnhancementResult
    from quality_control.effective import review_required

    from .progress import _state_for_image

    _stage, state, *_rest = _state_for_image(image)
    if state in ("queued", "processing") or ImageEnhancementResult.objects.filter(image=image, status__in=ACTIVE_STATUSES).exists():
        return STILL_PROCESSING
    if review_required(image):
        return IN_REVIEW
    return None


def move_to_trash(images, *, now=None):
    """Move ``images`` (their owner's visible photographs) to the trash; returns ``{"trashed": [ImageUpload], "skipped": {image id: reason}}``.

    One transaction: either every photograph that can be moved is, with its folder and package effects, or none is.
    """
    from identities.models import IdentityMembership
    from identities.services import TRASH_EMPTY_REASON, remove_photo_from_folders

    now = now or timezone.now()
    wanted = [image.pk for image in images]
    trashed, skipped = [], {}
    with transaction.atomic():
        # of=("self",): lock the photographs' own rows only (PostgreSQL refuses FOR UPDATE on the nullable side of a join).
        locked = list(
            ImageUpload.objects.select_for_update(of=("self",)).select_related("batch__organization", "processing_progress", "quality_decision").filter(pk__in=wanted)
        )
        group_ids, batch_ids = set(), set()
        for image in locked:
            reason = blocked_reason(image)
            if reason:
                skipped[image.pk] = reason
                continue
            snapshot = {
                str(row.pk): row.state
                for row in IdentityMembership.objects.filter(identity_face__image=image).exclude(state__in=[IdentityMembership.State.REMOVED, IdentityMembership.State.REJECTED])
            }
            group_ids.update(IdentityMembership.objects.filter(identity_face__image=image).exclude(identity_group__isnull=True).values_list("identity_group_id", flat=True))
            outcome = remove_photo_from_folders(image, reason="The photo was moved to the trash.", archive_reason=TRASH_EMPTY_REASON)
            TrashEntry.objects.update_or_create(
                image=image, defaults={"owner_id": image.owner_id, "memberships": snapshot, "archived_groups": outcome["archived"]},
            )
            ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=now)
            image.trashed_at = now
            batch_ids.add(image.batch_id)
            trashed.append(image)
        if trashed:
            expire_ready_copies(batch_ids, group_ids, now=now)
    if trashed:
        logger.info("Moved %d photo(s) to the trash (%d skipped).", len(trashed), len(skipped))
    return {"trashed": trashed, "skipped": skipped}


def restore(images, *, now=None):
    """Take ``images`` (photographs in their owner's trash) out of it; returns ``{"restored": [ImageUpload], "folders_restored": n}``.

    Folder memberships the trash removed are put back to the state they had, unless the folder is gone or no longer active, or the face has meanwhile been
    placed elsewhere: those stay out of the folder (the photograph itself is back either way).
    """
    from identities.models import IdentityChange, IdentityGroup, IdentityMembership
    from identities.services import TRASH_EMPTY_REASON, refresh_group

    wanted = [image.pk for image in images]
    restored, put_back = [], 0

    def archived_by_the_trash(group, entry):
        """Whether ``group`` is archived because moving photographs to the trash emptied it (by this entry or by any other photograph's)."""
        if group.state != IdentityGroup.State.ARCHIVED:
            return False
        if group.pk in (entry.archived_groups or []):
            return True
        last = IdentityChange.objects.filter(group=group, action=IdentityChange.Action.ARCHIVED).order_by("-created_at", "-pk").first()
        return last is not None and last.reason == TRASH_EMPTY_REASON
    with transaction.atomic():
        locked = list(ImageUpload.all_objects.select_for_update(of=("self",)).filter(pk__in=wanted, trashed_at__isnull=False))
        touched = set()
        for image in locked:
            entry = TrashEntry.objects.filter(image=image).first()
            for membership_id, state in ((entry.memberships if entry else None) or {}).items():
                membership = (
                    IdentityMembership.objects.select_for_update(of=("self",)).select_related("identity_group")
                    .filter(pk=int(membership_id), state__in=[IdentityMembership.State.REMOVED, IdentityMembership.State.REJECTED]).first()
                )
                if membership is None:
                    continue
                group = membership.identity_group
                if group is not None and archived_by_the_trash(group, entry):
                    group.state = IdentityGroup.State.ACTIVE
                    group.save(update_fields=["state", "updated_at"])
                if group is not None and group.state != IdentityGroup.State.ACTIVE:
                    continue
                if state in ("auto_matched", "new_group", "confirmed") and IdentityMembership.objects.filter(
                    identity_face=membership.identity_face_id, state__in=["auto_matched", "new_group", "confirmed"],
                ).exists():
                    continue
                membership.state = state
                membership.save(update_fields=["state", "updated_at"])
                put_back += 1
                if group is not None:
                    touched.add(group.pk)
            ImageUpload.all_objects.filter(pk=image.pk).update(trashed_at=None)
            image.trashed_at = None
            if entry is not None:
                entry.delete()
            restored.append(image)
        for group in IdentityGroup.objects.filter(pk__in=touched):
            refresh_group(group)
    if restored:
        logger.info("Restored %d photo(s) from the trash (%d folder membership(s) put back).", len(restored), put_back)
    return {"restored": restored, "folders_restored": put_back}


def purge(images):
    """Delete ``images`` (photographs in the trash) for good: the permanent delete, with all its effects."""
    return delete_images(list(images))


def purge_expired(*, now=None, limit=None):
    """Delete every photograph that has been in the trash longer than the retention, at most ``limit`` of them; returns how many were deleted."""
    now = now or timezone.now()
    cutoff = now - retention()
    expired = ImageUpload.all_objects.in_trash().filter(trashed_at__lt=cutoff).order_by("trashed_at", "pk")
    deleted = 0
    while limit is None or deleted < limit:
        size = PURGE_BATCH_SIZE if limit is None else min(PURGE_BATCH_SIZE, limit - deleted)
        chunk = list(expired[:size])
        if not chunk:
            break
        deleted += purge(chunk)["deleted"]
    if deleted:
        logger.info("Purged %d photo(s) that had been in the trash for more than %d days.", deleted, settings.TRASH_RETENTION_DAYS)
    return deleted

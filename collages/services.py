import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from events.services import get_event
from identities.models import IdentityGroup, IdentityMembership
from uploads.models import ImageUpload

from .layouts import MAX_PHOTOS, MIN_PHOTOS, evenly_spaced
from .models import Collage

logger = logging.getLogger(__name__)

ACTIVE_MEMBERSHIP_STATES = ("auto_matched", "new_group", "confirmed")
ACTIVE_STATUSES = (Collage.Status.QUEUED, Collage.Status.GENERATING)


class CollageNotPossible(ValueError):
    """The request cannot produce a collage (too few usable photos, no consent)."""


class SourceNotFound(LookupError):
    """The event or person does not exist for this owner."""


def usable_images(owner):
    """The owner's uploaded photos whose standardized image is ready to crop.

    Face boxes are stored in the coordinates of the standardized image, so that
    is the image a collage is cut from.
    """
    return (
        ImageUpload.objects.filter(owner=owner, status="uploaded", preprocessing_result__status="completed")
        .exclude(preprocessing_result__processed_file="")
    )


def _ordered_usable_ids(owner, candidate_ids):
    return list(
        usable_images(owner).filter(pk__in=candidate_ids)
        .order_by("created_at", "id").values_list("id", flat=True)
    )


def select_collage_photos(owner, source_type, source_ref, gap_hours=None):
    """Return ``(image_ids, subject)`` for a new collage, or raise.

    A person collage only reads the memberships that already exist for the
    owner's identity groups. It never computes an embedding or runs matching.
    """
    subject = ""
    if source_type == Collage.SourceType.EVENT:
        event = get_event(owner, source_ref, gap_hours)
        if event is None:
            raise SourceNotFound("event")
        candidate_ids = event["image_ids"]
    elif source_type == Collage.SourceType.PERSON:
        from privacy.services import biometric_processing_allowed

        if not biometric_processing_allowed(owner):
            raise CollageNotPossible("Biometric consent is required to make a collage of a person.")
        group = IdentityGroup.objects.filter(pk=source_ref, owner=owner, state=IdentityGroup.State.ACTIVE).first()
        if group is None:
            raise SourceNotFound("person")
        subject = group.display_name
        candidate_ids = list(
            IdentityMembership.objects.filter(identity_group=group, state__in=ACTIVE_MEMBERSHIP_STATES)
            .values_list("identity_face__image_id", flat=True)
        )
    else:
        raise CollageNotPossible("Unsupported collage source.")

    usable = _ordered_usable_ids(owner, candidate_ids)
    if len(usable) < MIN_PHOTOS:
        raise CollageNotPossible(
            f"At least {MIN_PHOTOS} processed photos are needed for a collage; found {len(usable)}."
        )
    return evenly_spaced(usable, MAX_PHOTOS), subject


def enqueue_collage(*, owner, source_type, source_ref, image_ids, title="", subject=""):
    from .tasks import generate_collage_task

    try:
        with transaction.atomic():
            active = Collage.objects.select_for_update().filter(owner=owner, status__in=ACTIVE_STATUSES).first()
            if active:
                return active, False
            task_id = uuid.uuid4().hex
            collage = Collage.objects.create(
                owner=owner, source_type=source_type, source_ref=source_ref,
                title=title[:80], image_ids=list(image_ids),
                manifest={"subject": subject} if subject else {},
                status=Collage.Status.QUEUED, task_id=task_id,
            )

            def dispatch():
                try:
                    generate_collage_task.apply_async(args=[collage.pk], task_id=task_id)
                except Exception:
                    logger.exception("Could not queue collage_id=%s.", collage.pk)
                    Collage.objects.filter(pk=collage.pk).update(
                        status=Collage.Status.FAILED, failure_reason="The collage queue is unavailable.",
                    )

            transaction.on_commit(dispatch)
    except IntegrityError:
        active = Collage.objects.filter(owner=owner, status__in=ACTIVE_STATUSES).first()
        if active:
            return active, False
        raise
    collage.refresh_from_db()
    return collage, True


def collage_expiration_time():
    return timezone.now() + timedelta(hours=settings.COLLAGE_TTL_HOURS)


def expire_collage_if_needed(collage, now=None):
    now = now or timezone.now()
    if collage.status == Collage.Status.READY and collage.expires_at and collage.expires_at <= now:
        if collage.collage_file:
            collage.collage_file.delete(save=False)
        collage.status = Collage.Status.EXPIRED
        collage.collage_file = ""
        collage.file_size = 0
        collage.save(update_fields=["status", "collage_file", "file_size", "updated_at"])
        return True
    return False


def expire_due_collages(*, owner=None, now=None):
    now = now or timezone.now()
    queryset = Collage.objects.filter(status=Collage.Status.READY, expires_at__lte=now)
    if owner is not None:
        queryset = queryset.filter(owner=owner)
    return sum(int(expire_collage_if_needed(collage, now=now)) for collage in queryset.iterator())

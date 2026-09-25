import logging

import cv2
import numpy as np
from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from .layouts import MAX_PHOTOS, MIN_PHOTOS
from .models import Collage
from .rendering import render_collage
from .services import collage_expiration_time, expire_due_collages, usable_images

# Transient infrastructure failures only (dropped DB connections). Rendering
# errors are handled below and must never retry.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)

logger = logging.getLogger(__name__)


class CollageBuildError(Exception):
    pass


def _read_standardized(preprocessing):
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise CollageBuildError("A photo could not be decoded.")
    return decoded


def build_collage(collage):
    """Return ``(jpeg_bytes, manifest)``. Reads only photos and face boxes."""
    if collage.source_type == Collage.SourceType.PERSON:
        from privacy.services import biometric_processing_allowed

        # Consent can be withdrawn between the request and the job running.
        if not biometric_processing_allowed(collage.owner):
            raise CollageBuildError("Biometric consent is no longer active.")

    images = {
        image.pk: image
        for image in usable_images(collage.owner).filter(pk__in=collage.image_ids)
        .select_related("preprocessing_result", "face_validation")
    }
    ordered = [images[image_id] for image_id in collage.image_ids if image_id in images][:MAX_PHOTOS]
    if len(ordered) < MIN_PHOTOS:
        raise CollageBuildError("Too few of the selected photos are still available.")

    tiles = []
    for image in ordered:
        pixels = _read_standardized(image.preprocessing_result)
        try:
            faces = image.face_validation.faces
        except Exception:
            faces = []
        tiles.append((pixels, faces))
    payload, crops = render_collage(tiles)
    manifest = {
        **{key: value for key, value in collage.manifest.items() if key == "subject"},
        "photos": [
            {
                "image_id": image.pk,
                "crop": {key: crop[key] for key in ("x", "y", "width", "height")},
                "faces_in_frame": crop["faces_in_frame"],
            }
            for image, crop in zip(ordered, crops)
        ],
    }
    return payload, manifest


def process_collage(collage_id, task_id=""):
    with transaction.atomic():
        collage = Collage.objects.select_for_update().filter(pk=collage_id).first()
        if collage is None:
            return None
        if collage.status == Collage.Status.READY:
            return collage
        if collage.task_id and task_id and collage.task_id != task_id:
            return collage
        collage.status = Collage.Status.GENERATING
        collage.failure_reason = ""
        collage.save(update_fields=["status", "failure_reason", "updated_at"])

    try:
        collage = Collage.objects.select_related("owner").get(pk=collage_id)
        payload, manifest = build_collage(collage)
        with transaction.atomic():
            locked = Collage.objects.select_for_update().filter(pk=collage_id).first()
            if locked is None:
                return None
            if locked.task_id and task_id and locked.task_id != task_id:
                return locked
            if locked.collage_file:
                locked.collage_file.delete(save=False)
            locked.collage_file.save("collage.jpg", ContentFile(payload), save=False)
            locked.manifest = manifest
            locked.file_size = len(payload)
            locked.status = Collage.Status.READY
            locked.failure_reason = ""
            locked.generated_at = timezone.now()
            locked.expires_at = collage_expiration_time()
            locked.save()
            return locked
    except Exception as error:
        if isinstance(error, CollageBuildError):
            logger.warning("Collage_id=%s not generated: %s", collage_id, error)
        else:
            logger.exception("Could not generate collage_id=%s.", collage_id)
        reason = str(error) if isinstance(error, CollageBuildError) else "The collage could not be generated."
        with transaction.atomic():
            failed = Collage.objects.select_for_update().filter(pk=collage_id).first()
            if failed is None:
                return None
            if failed.collage_file:
                failed.collage_file.delete(save=False)
            failed.collage_file = ""
            failed.status = Collage.Status.FAILED
            failed.failure_reason = reason
            failed.file_size = 0
            failed.expires_at = None
            failed.save()
            return failed


@shared_task(
    bind=True,
    name="collages.generate",
    acks_late=True,
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def generate_collage_task(self, collage_id):
    collage = Collage.objects.filter(pk=collage_id).first()
    if collage is None:
        return {"collage_id": collage_id, "status": "deleted"}
    if collage.task_id and self.request.id and collage.task_id != self.request.id:
        return {"collage_id": collage_id, "status": "superseded"}
    collage = process_collage(collage_id, self.request.id or "")
    return {"collage_id": collage_id, "status": collage.status if collage is not None else "deleted"}


@shared_task(
    name="collages.cleanup_expired",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_expired_collages():
    return {"expired_collages": expire_due_collages()}

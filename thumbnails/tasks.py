import logging

from celery import shared_task
from django.db.utils import InterfaceError, OperationalError

from uploads.models import ImageUpload

from .services import ThumbnailUnavailable, allowed_sizes, default_size, get_or_create_thumbnail

# Transient infrastructure failures only (dropped DB connections). A photo that
# cannot be read is not retried: the endpoint will simply report it as missing.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)

logger = logging.getLogger(__name__)


def pregenerate_thumbnail(image_id, size=None):
    """Create the default-size thumbnail now so the first gallery view finds it cached.

    Best effort: the lazy path in the endpoint still works if this never runs.
    Returns the thumbnail, or None when there is nothing to make.
    """
    size = size or default_size()
    if size not in allowed_sizes():
        return None
    image = ImageUpload.objects.filter(pk=image_id, status="uploaded").exclude(original_file="").first()
    if image is None:
        return None
    try:
        return get_or_create_thumbnail(image, size)
    except ThumbnailUnavailable:
        logger.info("No thumbnail for image_id=%s: the photo could not be read.", image_id)
        return None


@shared_task(
    name="thumbnails.generate",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def generate_thumbnail_task(image_id, size=None):
    thumbnail = pregenerate_thumbnail(image_id, size)
    return {"image_id": image_id, "generated": thumbnail is not None}

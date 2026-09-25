import logging

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from uploads.models import ImageUpload

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ImageUpload)
def queue_thumbnail_after_upload(sender, instance, created, raw=False, **kwargs):
    """Make the small thumbnail in the background as soon as a photo is stored."""
    if raw or not created or not settings.THUMBNAIL_PREGENERATE:
        return
    if instance.status != "uploaded" or not instance.original_file:
        return
    image_id = instance.pk

    def dispatch():
        from .tasks import generate_thumbnail_task

        try:
            generate_thumbnail_task.delay(image_id)
        except Exception:
            # Never let a queue problem affect an upload; the endpoint makes it on demand.
            logger.warning("Could not queue a thumbnail for image_id=%s.", image_id, exc_info=True)

    transaction.on_commit(dispatch)

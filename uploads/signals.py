from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from enhancement.models import ImageEnhancementResult
from quality_control.models import ImageQualityDecision
from validation.models import FaceValidationResult

from .models import ImagePreprocessingResult, ImageUpload
from .progress import ensure_image_progress, sync_image_progress


def _sync_after_commit(image_id):
    def synchronize():
        try:
            sync_image_progress(image_id)
        except ImageUpload.DoesNotExist:
            return
    transaction.on_commit(synchronize)


@receiver(post_save, sender=ImageUpload)
def create_upload_progress(sender, instance, **kwargs):
    ensure_image_progress(instance)
    _sync_after_commit(instance.pk)


@receiver(post_save, sender=ImagePreprocessingResult)
@receiver(post_save, sender=FaceValidationResult)
@receiver(post_save, sender=ImageQualityDecision)
@receiver(post_save, sender=ImageEnhancementResult)
def synchronize_processing_progress(sender, instance, **kwargs):
    _sync_after_commit(instance.image_id)

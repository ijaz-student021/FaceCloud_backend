"""A photograph the checks reject leaves its person folders.

The face check and the quality decision each end by saving a decision. When that decision is ``rejected`` the photograph is taken out of every person folder
it was grouped into (``services.remove_photo_from_folders``): the identity analysis often runs before the quality decision finishes, so a photo can be
grouped first and rejected a moment later. A failure here never stops the check that rejected the photograph.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from quality_control.models import ImageQualityDecision
from validation.models import FaceValidationResult

logger = logging.getLogger(__name__)


def _leave_folders(instance, source):
    from .services import remove_photo_from_folders

    try:
        with transaction.atomic():
            remove_photo_from_folders(instance.image, reason=f"The photo was rejected by the {source}.")
    except Exception:
        logger.exception("Could not take rejected image_id=%s out of its person folders.", instance.image_id)


@receiver(post_save, sender=FaceValidationResult)
def face_check_rejected(sender, instance, raw=False, **kwargs):
    if not raw and instance.decision == FaceValidationResult.Decision.REJECTED:
        _leave_folders(instance, "face check")


@receiver(post_save, sender=ImageQualityDecision)
def quality_decision_rejected(sender, instance, raw=False, **kwargs):
    if not raw and instance.decision == ImageQualityDecision.Decision.REJECTED:
        _leave_folders(instance, "quality check")

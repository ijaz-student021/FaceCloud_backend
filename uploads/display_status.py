"""The one status word a photo shows in the app, shared by My Images, batch responses and batch progress."""
from django.db.models import Prefetch

from .models import ImagePreprocessingResult


def with_status_relations(queryset):
    """Load everything ``image_display_status`` reads, so a page of photos costs a fixed number of queries."""
    from enhancement.models import ImageEnhancementResult

    return queryset.select_related("preprocessing_result", "face_validation", "quality_decision", "auto_enhancement").prefetch_related(
        Prefetch(
            "enhancement_results",
            queryset=ImageEnhancementResult.objects.order_by("-created_at"),
            to_attr="ordered_enhancement_results",
        )
    )


def latest_result(image):
    prefetched = getattr(image, "ordered_enhancement_results", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return image.enhancement_results.order_by("-created_at").first()


def image_display_status(image):
    from quality_control.models import ImageQualityDecision
    from validation.models import FaceValidationResult

    if image.status == "rejected":
        return "rejected"
    try:
        quality = image.quality_decision
    except ImageQualityDecision.DoesNotExist:
        quality = None
    if quality:
        if quality.status in {"pending", "queued", "processing", "failed"}:
            return "quality_failed" if quality.status == "failed" else "quality_processing"
        if quality.decision in {"rejected", "needs_review"}:
            return quality.decision
    try:
        validation = image.face_validation
    except FaceValidationResult.DoesNotExist:
        validation = None
    if validation and validation.decision in {"rejected", "needs_review", "failed"}:
        return validation.decision
    result = latest_result(image)
    if result is not None:
        return result.status
    if quality and quality.status == "completed" and quality.decision == "accepted":
        return "accepted"
    try:
        preprocessing = image.preprocessing_result
    except ImagePreprocessingResult.DoesNotExist:
        return "uploaded"
    if preprocessing.status == "completed":
        return "ready"
    return "preprocessing" if preprocessing.status in {"pending", "processing"} else "failed"

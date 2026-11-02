import time
import uuid

import cv2
import numpy as np
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from enhancement.face_sharpness import primary_face_detail
from enhancement.services import analyze_image_quality
from uploads.models import ImagePreprocessingResult
from validation.models import FaceValidationResult

from .models import ImageQualityDecision


def _bounded(value):
    return round(max(0.0, min(100.0, float(value))), 2)


def _read_standardized(preprocessing):
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("Preprocessing must complete before quality analysis.")
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValidationError("The standardized image could not be decoded for quality analysis.")
    return image


def primary_face(face_validation):
    """The face the photograph is judged on: the largest confidently detected one, as ``(x, y, width, height)``; ``None`` without one."""
    faces = [
        face for face in (getattr(face_validation, "faces", None) or [])
        if (face.get("confidence") or 0) >= settings.QUALITY_FACE_MIN_CONFIDENCE and face.get("width") and face.get("height")
    ]
    if not faces:
        return None
    face = max(faces, key=lambda item: item.get("area_ratio") or item["width"] * item["height"])
    return face["x"], face["y"], face["width"], face["height"]


def with_face_sharpness(metrics, image, face_validation):
    """``metrics`` with sharpness measured on the primary face when there is one (and the frame's own value kept alongside).

    ``sharpness_basis`` says which measure ``laplacian_sharpness`` now holds, so a stored decision is never ambiguous about it.
    """
    metrics = dict(metrics)
    metrics["frame_laplacian_sharpness"] = metrics["laplacian_sharpness"]
    metrics["sharpness_basis"] = "frame"
    box = primary_face(face_validation)
    detail = primary_face_detail(image, box, settings.QUALITY_FACE_REGION_INSET) if box else None
    if detail is not None:
        metrics["laplacian_sharpness"] = detail
        metrics["sharpness_basis"] = "face"
        metrics["face_blur_warning"] = detail < settings.QUALITY_FACE_BLUR_WARNING
    return metrics


def calculate_scores(metrics):
    luminance = metrics["mean_luminance"]
    brightness = _bounded(100 - abs(luminance - settings.QUALITY_IDEAL_LUMINANCE) * 0.8)
    target = settings.QUALITY_FACE_SHARPNESS_TARGET if metrics.get("sharpness_basis") == "face" else settings.QUALITY_SHARPNESS_TARGET
    sharpness = _bounded(metrics["laplacian_sharpness"] / target * 100)
    contrast = _bounded(metrics["contrast_stddev"] / settings.QUALITY_CONTRAST_TARGET * 100)
    noise = _bounded(100 - metrics["estimated_noise"] / settings.QUALITY_MAX_NOISE * 100)
    clipping = metrics["shadow_clipping_percent"] + metrics["highlight_clipping_percent"]
    exposure = _bounded(100 - clipping / settings.QUALITY_MAX_TOTAL_CLIPPING * 100)
    shortest_edge = min(metrics["width"], metrics["height"])
    resolution = _bounded(shortest_edge / settings.QUALITY_RESOLUTION_TARGET * 100)
    overall = _bounded(
        brightness * 0.20 + sharpness * 0.25 + contrast * 0.15
        + noise * 0.10 + exposure * 0.15 + resolution * 0.15
    )
    return {
        "brightness_score": brightness, "sharpness_score": sharpness,
        "contrast_score": contrast, "noise_score": noise,
        "exposure_score": exposure, "resolution_score": resolution,
        "overall_score": overall,
    }


def acceptance_score(metrics, scores):
    """The number compared with QUALITY_ACCEPT_SCORE: the overall score, without its sharpness term when the face was measured, so blur
    alone cannot pull a photograph that has a face below the line (the sharpness score itself is still stored and shown)."""
    if metrics.get("sharpness_basis") == "face":
        return _bounded((scores["overall_score"] - scores["sharpness_score"] * 0.25) / 0.75)
    return scores["overall_score"]


def photo_has_face(face_validation):
    """Whether a face was found (whatever the verdict on it): such a photograph is never refused or held back for blur or resolution."""
    return face_validation is not None and (face_validation.face_count or 0) > 0


def make_decision(metrics, scores, face_validation):
    reject = []
    review = []
    shortest = min(metrics["width"], metrics["height"])
    has_face = photo_has_face(face_validation)
    # Blur and a low resolution are what the restoration chain (deblur, face restoration, upscale to HD) exists for. With a face they
    # never reject or review a photograph; without one the frame's own sharpness still counts, as there is nothing else to work with.
    if not has_face:
        if metrics["laplacian_sharpness"] < settings.QUALITY_REJECT_SHARPNESS:
            reject.append("severe_blur")
        elif metrics["laplacian_sharpness"] < settings.QUALITY_REVIEW_SHARPNESS:
            review.append("borderline_blur")
    if shortest < settings.QUALITY_REJECT_MIN_EDGE:
        reject.append("resolution_too_low")
    elif shortest < settings.QUALITY_REVIEW_MIN_EDGE and not has_face:
        review.append("borderline_resolution")
    # Darkness alone does not refuse a photograph: the low-light enhancement exists for it. It is refused ("severe_underexposure") only when it is practically black, so there is
    # nothing to lift; between that and the review line it carries the warning "underexposed".
    if metrics["mean_luminance"] < settings.QUALITY_REJECT_MIN_LUMINANCE:
        reject.append("severe_underexposure")
    elif metrics["mean_luminance"] < settings.QUALITY_REVIEW_MIN_LUMINANCE:
        review.append("underexposed")
    if metrics["mean_luminance"] > settings.QUALITY_REJECT_MAX_LUMINANCE:
        reject.append("severe_overexposure")
    elif metrics["mean_luminance"] > settings.QUALITY_REVIEW_MAX_LUMINANCE:
        review.append("overexposed")
    # Contrast is measured in grey levels, so a dark photograph has little of it by nature (a quarter of the light is a quarter of the spread). Below the review line the floor shrinks
    # in proportion to the brightness, continuously, so "almost no contrast" still refuses a dark photograph without refusing one that is merely dark.
    dark_share = min(1.0, metrics["mean_luminance"] / settings.QUALITY_REVIEW_MIN_LUMINANCE) if settings.QUALITY_REVIEW_MIN_LUMINANCE > 0 else 1.0
    if metrics["contrast_stddev"] < settings.QUALITY_REJECT_CONTRAST * dark_share:
        reject.append("contrast_too_low")
    elif metrics["contrast_stddev"] < settings.QUALITY_REVIEW_CONTRAST:
        review.append("borderline_contrast")
    if metrics["estimated_noise"] > settings.QUALITY_REVIEW_NOISE:
        review.append("excessive_noise")

    if face_validation is None or face_validation.decision in {"needs_review", "failed"}:
        review.append("face_validation_required")
    elif face_validation.decision == "rejected":
        reject.append(face_validation.rejection_reason or "face_validation_rejected")

    reject = list(dict.fromkeys(reject))
    review = [reason for reason in dict.fromkeys(review) if reason not in reject]
    if reject:
        return "rejected", reject + review, "The image did not pass mandatory quality checks."
    if review or acceptance_score(metrics, scores) < settings.QUALITY_ACCEPT_SCORE:
        reasons = review or ["overall_score_below_acceptance"]
        return "needs_review", reasons, "The image requires review before enhancement."
    return "accepted", [], "The image passed quality and face checks."


@transaction.atomic
def process_quality_decision(decision_id):
    result = ImageQualityDecision.objects.select_for_update().select_related(
        "preprocessing_result", "image", "owner"
    ).get(pk=decision_id)
    if result.status == ImageQualityDecision.Status.COMPLETED:
        return result
    result.status = ImageQualityDecision.Status.PROCESSING
    result.started_at = result.started_at or timezone.now()
    result.failure_reason = ""
    result.save(update_fields=["status", "started_at", "failure_reason", "updated_at"])
    started = time.perf_counter()
    try:
        image = _read_standardized(result.preprocessing_result)
        metrics = with_face_sharpness(analyze_image_quality(image), image, result.face_validation)
        scores = calculate_scores(metrics)
        decision, reasons, message = make_decision(metrics, scores, result.face_validation)
        for field, value in scores.items():
            setattr(result, field, value)
        result.measurements = metrics
        result.defects = metrics["defects"]
        result.rejection_reasons = reasons
        result.decision = decision
        result.message = message
        result.status = ImageQualityDecision.Status.COMPLETED
        result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        result.finished_at = timezone.now()
        result.save()
        from quality_control.effective import moderator_can_decide

        if decision == "needs_review" and moderator_can_decide(result.image):      # a personal workspace has no moderator: nothing to queue
            from moderation.services import create_review_for_quality_decision
            create_review_for_quality_decision(result)
        # Person folders follow the verdict: queued here, in the same transaction as the decision, so the photograph's progress never shows it finished in
        # between (accepted: now; rejected: never; needs_review: now in a personal workspace, else after a moderator approves it). Never raises.
        from identities.verdict import group_after_verdict
        group_after_verdict(result.image, verdict=decision)
        # The photograph is enhanced automatically on the same terms (enhancement/auto.py): accepted now, rejected never, needs_review now in a personal
        # workspace, else after a moderator approves it. Same transaction as the decision, never raises, and never makes a second enhancement.
        from enhancement.auto import enhance_after_verdict
        enhance_after_verdict(result.image, verdict=decision)
        # Search by what is in the photograph indexes it once the verdict is committed (only when that feature is switched on; accepted photographs only).
        from search.semantic.service import index_after_verdict
        index_after_verdict(result.image)
        return result
    except Exception as error:
        result.status = ImageQualityDecision.Status.FAILED
        result.failure_reason = "Quality analysis failed safely."
        result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        result.finished_at = timezone.now()
        result.save(update_fields=[
            "status", "failure_reason", "processing_duration_ms", "finished_at", "updated_at",
        ])
        raise ValidationError("Quality analysis failed safely.") from error


def create_or_reset_quality_decision(image):
    try:
        preprocessing = image.preprocessing_result
    except ImagePreprocessingResult.DoesNotExist as error:
        raise ValidationError("Preprocessing must complete before quality analysis.") from error
    try:
        face_validation = image.face_validation
    except FaceValidationResult.DoesNotExist:
        face_validation = None
    result, _created = ImageQualityDecision.objects.update_or_create(
        image=image,
        defaults={
            "owner": image.owner, "preprocessing_result": preprocessing,
            "face_validation": face_validation, "status": ImageQualityDecision.Status.PENDING,
            "decision": "", "failure_reason": "", "task_id": "",
        },
    )
    return result


def enqueue_quality_analysis(image):
    from .tasks import analyze_image_quality_task

    result = create_or_reset_quality_decision(image)
    task_id = uuid.uuid4().hex
    result.status = ImageQualityDecision.Status.QUEUED
    result.task_id = task_id
    result.save(update_fields=["status", "task_id", "updated_at"])
    def dispatch():
        try:
            analyze_image_quality_task.apply_async(args=[result.pk], task_id=task_id)
        except Exception:
            ImageQualityDecision.objects.filter(pk=result.pk).update(
                status=ImageQualityDecision.Status.FAILED,
                failure_reason="The quality-analysis queue is unavailable.",
                finished_at=timezone.now(),
            )

    transaction.on_commit(dispatch)
    result.refresh_from_db()
    return result

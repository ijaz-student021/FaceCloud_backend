"""OpenCV enhancement services and parameter policies.

Lighting correction is applied to the LAB luminance channel. Colour channels are
left unchanged so the baseline does not intentionally normalize skin tones.
"""

import hashlib
import time
import threading
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult
from quality_control.models import ImageQualityDecision

from .models import EnhancementLevel, EnhancementStatus, ImageEnhancementResult
from .zero_dce import ZeroDCEUnavailable, enhance_with_zero_dce


LEVEL_POLICIES = {
    EnhancementLevel.LOW: {
        "gamma": 0.96, "clahe_clip": 1.1, "clahe_grid": 8,
        "blend": 0.25, "sharpen": 0.06, "denoise": 0,
    },
    EnhancementLevel.MEDIUM: {
        "gamma": 0.88, "clahe_clip": 1.35, "clahe_grid": 8,
        "blend": 0.40, "sharpen": 0.10, "denoise": 0,
    },
    EnhancementLevel.HIGH: {
        "gamma": 0.80, "clahe_clip": 1.55, "clahe_grid": 8,
        "blend": 0.52, "sharpen": 0.14, "denoise": 0,
    },
}

_SUPERRES_LOCK = threading.Lock()
_SUPERRES_CACHE = {}


def _auto_level(mean_luminance):
    if mean_luminance < 65:
        return EnhancementLevel.HIGH
    if mean_luminance < 115:
        return EnhancementLevel.MEDIUM
    return EnhancementLevel.LOW


def _gamma_lut(gamma):
    return np.array([((value / 255.0) ** gamma) * 255 for value in range(256)], dtype=np.uint8)


def analyze_image_quality(image):
    """Measure defects without classifying identity, ethnicity, or skin colour."""
    if image is None or image.size == 0:
        raise ValidationError("The standardized image cannot be analysed safely.")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    luminance = float(np.mean(gray))
    contrast = float(np.std(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    smooth = cv2.GaussianBlur(gray, (3, 3), 0)
    noise = float(np.median(np.abs(gray.astype(np.float32) - smooth.astype(np.float32))))
    shadow_clipping = float(np.mean(gray <= 5) * 100)
    highlight_clipping = float(np.mean(gray >= 250) * 100)
    height, width = gray.shape
    shortest_edge = min(width, height)

    defects = []
    if luminance < 65:
        defects.append("severe_low_light")
    elif luminance < 115:
        defects.append("low_light")
    if sharpness < 35:
        defects.append("severe_blur")
    elif sharpness < 90:
        defects.append("mild_blur")
    if noise > 5:
        defects.append("visible_noise")
    if contrast < 28:
        defects.append("low_contrast")
    if highlight_clipping > 8:
        defects.append("overexposed_highlights")
    if shortest_edge < 720:
        defects.append("low_resolution")

    restoration_required = any(
        defect in defects for defect in ("severe_blur", "low_resolution")
    )
    return {
        "width": width,
        "height": height,
        "mean_luminance": round(luminance, 2),
        "contrast_stddev": round(contrast, 2),
        "laplacian_sharpness": round(sharpness, 2),
        "estimated_noise": round(noise, 2),
        "shadow_clipping_percent": round(shadow_clipping, 3),
        "highlight_clipping_percent": round(highlight_clipping, 3),
        "defects": defects,
        "needs_ai_restoration": restoration_required,
    }


def compare_image_quality(source, output):
    """Compare images at the source pixel grid to avoid resolution-biased scores."""
    source_height, source_width = source.shape[:2]
    if output.shape[:2] != (source_height, source_width):
        comparable_output = cv2.resize(
            output,
            (source_width, source_height),
            interpolation=cv2.INTER_AREA,
        )
    else:
        comparable_output = output
    before = analyze_image_quality(source)
    after = analyze_image_quality(comparable_output)
    metric_names = (
        "mean_luminance",
        "contrast_stddev",
        "laplacian_sharpness",
        "estimated_noise",
        "shadow_clipping_percent",
        "highlight_clipping_percent",
    )
    return {
        "method": "source_resolution_normalized_v1",
        "reference_dimensions": [source_width, source_height],
        "before": before,
        "after": after,
        "deltas": {
            name: round(after[name] - before[name], 3)
            for name in metric_names
        },
    }


def _load_edsr_model(model_path, scale):
    key = (str(model_path), scale)
    if key in _SUPERRES_CACHE:
        return _SUPERRES_CACHE[key]
    if not hasattr(cv2, "dnn_superres"):
        raise RuntimeError("The OpenCV super-resolution module is not installed.")
    model_path = Path(model_path)
    if not model_path.is_file():
        raise RuntimeError("The configured EDSR model file is unavailable.")
    superres = cv2.dnn_superres.DnnSuperResImpl_create()
    superres.readModel(str(model_path))
    superres.setModel("edsr", scale)
    _SUPERRES_CACHE[key] = superres
    return superres


def restore_low_resolution(image, quality):
    """Apply bounded pretrained EDSR only when the source is low resolution."""
    metadata = {
        "requested": "low_resolution" in quality["defects"],
        "applied": False,
        "provider": "opencv_dnn_edsr",
        "scale": settings.AI_RESTORATION_SCALE,
        "fallback_reason": "",
    }
    if not metadata["requested"]:
        return image, metadata
    if not settings.AI_RESTORATION_ENABLED:
        metadata["fallback_reason"] = "AI restoration is disabled."
        return image, metadata
    height, width = image.shape[:2]
    output_pixels = (
        width * settings.AI_RESTORATION_SCALE
        * height * settings.AI_RESTORATION_SCALE
    )
    if output_pixels > settings.AI_RESTORATION_MAX_OUTPUT_PIXELS:
        metadata["fallback_reason"] = "The requested restored output exceeds the safety limit."
        return image, metadata
    try:
        with _SUPERRES_LOCK:
            model = _load_edsr_model(
                settings.AI_RESTORATION_EDSR_MODEL_PATH,
                settings.AI_RESTORATION_SCALE,
            )
            restored = model.upsample(image)
        if restored is None or restored.size == 0:
            raise RuntimeError("The restoration model returned no output.")
        metadata["applied"] = True
        metadata["input_dimensions"] = [width, height]
        metadata["output_dimensions"] = [int(restored.shape[1]), int(restored.shape[0])]
        return restored, metadata
    except (RuntimeError, cv2.error, OSError) as error:
        metadata["fallback_reason"] = str(error)
        return image, metadata


def _adapt_policy(base_policy, quality):
    policy = dict(base_policy)
    defects = quality["defects"]
    if "visible_noise" in defects:
        policy["denoise"] = 2
    if "mild_blur" in defects:
        policy["sharpen"] = min(policy["sharpen"] + 0.06, 0.20)
    if "severe_blur" in defects:
        # Strong sharpening creates halos and cannot reconstruct missing pixels.
        policy["sharpen"] = min(policy["sharpen"] + 0.03, 0.16)
        policy["denoise"] = 0
    if "overexposed_highlights" in defects:
        policy["gamma"] = max(policy["gamma"], 0.94)
        policy["blend"] = min(policy["blend"], 0.25)
    return policy


def _quality_guard(original, candidate, level, input_quality):
    """Blend toward the original when enhancement becomes destructive."""
    output_quality = analyze_image_quality(candidate)
    max_luminance_gain = {
        EnhancementLevel.LOW: 14,
        EnhancementLevel.MEDIUM: 28,
        EnhancementLevel.HIGH: 42,
    }[level]
    luminance_gain = output_quality["mean_luminance"] - input_quality["mean_luminance"]
    clipping_growth = (
        output_quality["highlight_clipping_percent"]
        - input_quality["highlight_clipping_percent"]
    )
    fallback_applied = False
    guarded = candidate
    if luminance_gain > max_luminance_gain or clipping_growth > 3:
        guarded = cv2.addWeighted(original, 0.45, candidate, 0.55, 0)
        output_quality = analyze_image_quality(guarded)
        fallback_applied = True
    return guarded, output_quality, {
        "fallback_blend_applied": fallback_applied,
        "luminance_gain": round(
            output_quality["mean_luminance"] - input_quality["mean_luminance"], 2
        ),
        "highlight_clipping_growth": round(
            output_quality["highlight_clipping_percent"]
            - input_quality["highlight_clipping_percent"], 3
        ),
    }


def enhance_bgr(image, requested_level=EnhancementLevel.AUTO):
    """Return an enhanced BGR image, applied level, and auditable parameters."""
    if image is None or image.size == 0 or len(image.shape) != 3 or image.shape[2] != 3:
        raise ValidationError("The standardized image cannot be decoded safely.")
    if requested_level not in EnhancementLevel.values:
        raise ValidationError({"level": "Choose low, medium, high, or auto."})

    input_quality = analyze_image_quality(image)
    working_image, restoration = restore_low_resolution(image, input_quality)
    guard_reference = working_image.copy()
    zero_dce = {
        "requested": any(defect in input_quality["defects"] for defect in ("low_light", "severe_low_light")),
        "applied": False, "fallback_reason": "",
    }
    if zero_dce["requested"] and settings.ZERO_DCE_ENABLED:
        try:
            working_image, zero_metadata = enhance_with_zero_dce(working_image)
            zero_dce.update(zero_metadata)
            zero_dce["applied"] = True
        except ZeroDCEUnavailable as error:
            zero_dce["fallback_reason"] = str(error)
    elif zero_dce["requested"]:
        zero_dce["fallback_reason"] = "Zero-DCE is disabled."
    mean_luminance = input_quality["mean_luminance"]
    applied_level = _auto_level(mean_luminance) if requested_level == EnhancementLevel.AUTO else requested_level
    policy = _adapt_policy(LEVEL_POLICIES[applied_level], input_quality)
    if zero_dce["applied"]:
        enhanced = working_image
    else:
        lab = cv2.cvtColor(working_image, cv2.COLOR_BGR2LAB)
        luminance, channel_a, channel_b = cv2.split(lab)
        corrected = cv2.LUT(luminance, _gamma_lut(policy["gamma"]))
        clahe = cv2.createCLAHE(
            clipLimit=policy["clahe_clip"],
            tileGridSize=(policy["clahe_grid"], policy["clahe_grid"]),
        )
        corrected = clahe.apply(corrected)
        corrected = cv2.addWeighted(
            luminance, 1.0 - policy["blend"], corrected, policy["blend"], 0
        )
        enhanced = cv2.cvtColor(cv2.merge((corrected, channel_a, channel_b)), cv2.COLOR_LAB2BGR)

    if policy["denoise"]:
        enhanced = cv2.fastNlMeansDenoisingColored(
            enhanced, None, policy["denoise"], policy["denoise"], 7, 21
        )
    if policy["sharpen"]:
        blurred = cv2.GaussianBlur(enhanced, (0, 0), 1.0)
        enhanced = cv2.addWeighted(
            enhanced, 1.0 + policy["sharpen"], blurred, -policy["sharpen"], 0
        )

    enhanced, output_quality, guard = _quality_guard(
        guard_reference, enhanced, applied_level, analyze_image_quality(guard_reference)
    )
    normalized_comparison = compare_image_quality(image, enhanced)

    warnings = []
    if zero_dce["requested"] and not zero_dce["applied"]:
        warnings.append(
            "Zero-DCE was requested for low light but was unavailable; "
            "the adaptive OpenCV fallback was used."
        )
    if input_quality["needs_ai_restoration"] and not restoration["applied"]:
        warnings.append(
            "Severe blur or low resolution requires an AI restoration model; "
            "the baseline avoids inventing facial detail."
        )

    parameters = {
        **policy,
        "mean_input_luminance": round(mean_luminance, 2),
        "algorithm": "zero_dce_hybrid_v1" if zero_dce["applied"] else "opencv_adaptive_restoration_router_v2",
        "colour_policy": "learned_rgb_curves" if zero_dce["applied"] else "preserve_lab_chroma_channels",
        "quality_before": input_quality,
        "quality_after": output_quality,
        "quality_comparison": normalized_comparison,
        "quality_guard": guard,
        "restoration": restoration,
        "zero_dce": zero_dce,
        "warnings": warnings,
    }
    return enhanced, applied_level, parameters


def _read_standardized_image(preprocessing):
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("The image must finish preprocessing before enhancement.")
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValidationError("The standardized image cannot be decoded safely.")
    return decoded


def _encode_output(image, preprocessing):
    use_png = preprocessing.output_format == "png"
    extension = ".png" if use_png else ".jpg"
    output_format = "png" if use_png else "jpeg"
    options = (
        [cv2.IMWRITE_PNG_COMPRESSION, 6]
        if use_png
        else [cv2.IMWRITE_JPEG_QUALITY, settings.PREPROCESS_JPEG_QUALITY]
    )
    success, encoded = cv2.imencode(extension, image, options)
    if not success:
        raise ValidationError("The enhanced image could not be encoded safely.")
    return encoded.tobytes(), extension, output_format


def create_enhancement_result(image, requested_level=EnhancementLevel.AUTO, batch_job=None):
    if image.owner_id != (batch_job.owner_id if batch_job else image.owner_id):
        raise ValidationError("The enhancement owner does not match the image owner.")
    try:
        preprocessing = image.preprocessing_result
    except ImagePreprocessingResult.DoesNotExist as error:
        raise ValidationError("The image must finish preprocessing before enhancement.") from error
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("The image must finish preprocessing before enhancement.")
    try:
        quality = image.quality_decision
    except ImageQualityDecision.DoesNotExist:
        quality = None
    if quality and quality.status == "completed" and quality.decision != "accepted":
        raise ValidationError(
            "This image must be accepted by the quality workflow before enhancement."
        )
    return ImageEnhancementResult.objects.create(
        owner=image.owner,
        image=image,
        preprocessing_result=preprocessing,
        batch_job=batch_job,
        requested_level=requested_level,
        status=EnhancementStatus.PENDING,
    )


def process_enhancement_result(result):
    """Process one result and persist a separate enhanced output."""
    with transaction.atomic():
        result = (
            ImageEnhancementResult.objects.select_for_update()
            .select_related("preprocessing_result", "image", "owner")
            .get(pk=result.pk)
        )
        if result.status == EnhancementStatus.COMPLETED and result.enhanced_file:
            return result
        if result.status == EnhancementStatus.CANCELLED:
            return result
        result.status = EnhancementStatus.PROCESSING
        result.started_at = result.started_at or timezone.now()
        result.finished_at = None
        result.failure_reason = ""
        result.save(update_fields=["status", "started_at", "finished_at", "failure_reason", "updated_at"])

    started = time.perf_counter()
    stored_name = None
    try:
        source = _read_standardized_image(result.preprocessing_result)
        enhanced, applied_level, parameters = enhance_bgr(source, result.requested_level)
        payload, extension, output_format = _encode_output(enhanced, result.preprocessing_result)
        source_stem = Path(result.preprocessing_result.output_name or result.image.original_name).stem
        output_name = f"{source_stem}_enhanced_{applied_level}{extension}"
        result.enhanced_file.save(output_name, ContentFile(payload), save=False)
        stored_name = result.enhanced_file.name
        result.applied_level = applied_level
        result.status = EnhancementStatus.COMPLETED
        result.output_name = output_name
        result.output_format = output_format
        result.output_width = int(enhanced.shape[1])
        result.output_height = int(enhanced.shape[0])
        result.output_file_size = len(payload)
        result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        result.applied_parameters = parameters
        result.failure_reason = ""
        result.finished_at = timezone.now()
        result.save()
        return result
    except ValidationError:
        if stored_name:
            result.enhanced_file.storage.delete(stored_name)
        result.status = EnhancementStatus.FAILED
        result.failure_reason = "The image could not be enhanced safely."
        result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        result.finished_at = timezone.now()
        result.save(update_fields=["status", "failure_reason", "processing_duration_ms", "finished_at", "updated_at"])
        raise
    except (cv2.error, OSError, ValueError) as error:
        if stored_name:
            result.enhanced_file.storage.delete(stored_name)
        result.status = EnhancementStatus.FAILED
        result.failure_reason = "The image could not be enhanced safely."
        result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        result.finished_at = timezone.now()
        result.save(update_fields=["status", "failure_reason", "processing_duration_ms", "finished_at", "updated_at"])
        raise ValidationError("The image could not be enhanced safely.") from error

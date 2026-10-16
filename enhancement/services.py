"""OpenCV enhancement services and parameter policies.

Lighting correction is applied to the LAB luminance channel. Colour channels are
left unchanged so the baseline does not intentionally normalize skin tones.
"""

import logging
import time
import threading
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, ValidationError
from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError

from uploads.models import ImagePreprocessingResult
from quality_control.models import ImageQualityDecision

from .models import (
    EditMode,
    EnhancementBatchJob,
    EnhancementLevel,
    EnhancementStatus,
    EnhancementStyle,
    ImageEditVersion,
    ImageEnhancementResult,
)
from .colorization import ColorizationUnavailable, colorize_with_siggraph17
from .gfpgan import GFPGANUnavailable, restore_faces
from .identity_guard import IdentityGuardUnavailable, apply_identity_guard
from .hd_restoration import (
    HDRestorationUnavailable,
    deblur_with_nafnet,
    upscale_with_realesrgan,
)
from .purescale import PureScaleUnavailable, upscale_with_purescale
from .purevision import PureVisionUnavailable, restore_with_purevision
from .zero_dce import ZeroDCEUnavailable, enhance_with_zero_dce


logger = logging.getLogger(__name__)


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

# These limits are deliberately conservative.  Enhancement levels control how
# far the output may move from the source; they do not promise that every model
# candidate will be used at full strength.
OUTPUT_GUARD_POLICIES = {
    EnhancementLevel.LOW: {
        "learned_model_strength": 0.30,
        "max_luminance_gain": 10.0,
        "max_luminance_loss": 8.0,
        "minimum_contrast_ratio": 0.88,
        "max_highlight_clipping_growth": 1.0,
        "max_chroma_shift": 2.5,
    },
    EnhancementLevel.MEDIUM: {
        "learned_model_strength": 0.45,
        "max_luminance_gain": 18.0,
        "max_luminance_loss": 12.0,
        "minimum_contrast_ratio": 0.82,
        "max_highlight_clipping_growth": 1.5,
        "max_chroma_shift": 3.5,
    },
    EnhancementLevel.HIGH: {
        "learned_model_strength": 0.60,
        "max_luminance_gain": 30.0,
        "max_luminance_loss": 18.0,
        "minimum_contrast_ratio": 0.75,
        "max_highlight_clipping_growth": 2.0,
        "max_chroma_shift": 5.0,
    },
}

_SUPERRES_LOCK = threading.Lock()
_SUPERRES_CACHE = {}


@dataclass
class EnhancementPipelineContext:
    """Mutable state shared by the independently testable pipeline stages."""

    source_image: np.ndarray
    requested_level: str
    style: str = EnhancementStyle.NATURAL
    allow_biometric_processing: bool = True
    use_zero_dce: bool = True
    input_quality: dict = field(default_factory=dict)
    applied_level: str = ""
    policy: dict = field(default_factory=dict)
    current_image: np.ndarray | None = None
    final_guard_reference: np.ndarray | None = None
    output_quality: dict = field(default_factory=dict)
    restoration: dict = field(default_factory=dict)
    hd_restoration: dict = field(default_factory=dict)
    purevision: dict = field(default_factory=dict)
    purescale: dict = field(default_factory=dict)
    face_restoration: dict = field(default_factory=dict)
    colorization: dict = field(default_factory=dict)
    dynamic_style: dict = field(default_factory=dict)
    identity_guard: dict = field(default_factory=dict)
    zero_dce: dict = field(default_factory=dict)
    quality_guards: dict = field(default_factory=dict)
    post_exposure_noise: dict = field(default_factory=dict)
    adaptive_denoise: dict = field(default_factory=dict)
    resolution_contract: dict = field(default_factory=dict)
    stage_timings_ms: dict = field(default_factory=dict)
    stage_order: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        if self.current_image is None:
            self.current_image = self.source_image.copy()


@contextmanager
def _timed_pipeline_stage(context, stage_name):
    started_at = time.perf_counter()
    try:
        yield
    finally:
        context.stage_timings_ms[stage_name] = round(
            (time.perf_counter() - started_at) * 1000,
            3,
        )
        context.stage_order.append(stage_name)


def _auto_level(mean_luminance):
    if mean_luminance < 65:
        return EnhancementLevel.HIGH
    if mean_luminance < 115:
        return EnhancementLevel.MEDIUM
    return EnhancementLevel.LOW


def _gamma_lut(gamma):
    return np.array([((value / 255.0) ** gamma) * 255 for value in range(256)], dtype=np.uint8)


def _histogram_order_statistic(cumulative, index):
    """The value at sorted position ``index`` of a distribution given its cumulative counts."""
    return int(np.searchsorted(cumulative, index, side="right"))


def _histogram_percentile(cumulative, total, percentile):
    """``np.percentile(values, percentile)`` (linear interpolation) from a histogram.

    The virtual-index arithmetic and the interpolation mirror NumPy's own, so the
    result is identical to sorting every pixel, at a fraction of the cost.
    """
    quantile = percentile / 100
    virtual = (total - 1) * quantile  # NumPy's expression for method="linear"
    lower = min(max(int(np.floor(virtual)), 0), total - 1)
    upper = min(lower + 1, total - 1)
    gamma = virtual - lower
    low = _histogram_order_statistic(cumulative, lower)
    high = _histogram_order_statistic(cumulative, upper)
    difference = high - low
    if gamma >= 0.5:
        return float(high - difference * (1 - gamma))
    return float(low + difference * gamma)


def _histogram_median(cumulative, total):
    """``np.median(values)`` from a histogram (mean of the middle pair when even)."""
    if total % 2:
        return float(_histogram_order_statistic(cumulative, total // 2))
    low = _histogram_order_statistic(cumulative, total // 2 - 1)
    high = _histogram_order_statistic(cumulative, total // 2)
    return (low + high) / 2


def analyze_image_quality(image):
    """Measure defects without classifying identity, ethnicity, or skin colour.

    Everything except sharpness is derived from 256-bin histograms instead of
    passing over every pixel several times (a 12-megapixel photo took about 0.6 s,
    and the pipeline measures it around eight times). The results are identical to
    the straightforward per-pixel implementation, which the tests keep as the
    reference.
    """
    if image is None or image.size == 0:
        raise ValidationError("The standardized image cannot be analysed safely.")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    total = int(gray.size)
    histogram = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel().astype(np.int64)
    cumulative = np.cumsum(histogram)
    levels = np.arange(256, dtype=np.int64)
    luminance = float(int(np.dot(levels, histogram)) / total)
    contrast = float(np.sqrt(np.dot(histogram, (levels - luminance) ** 2) / total))
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness = float(cv2.meanStdDev(laplacian)[1][0][0] ** 2)
    smooth = cv2.GaussianBlur(gray, (3, 3), 0)
    residual = np.cumsum(
        cv2.calcHist([cv2.absdiff(gray, smooth)], [0], None, [256], [0, 256]).ravel().astype(np.int64)
    )
    noise = _histogram_median(residual, total)
    shadow_clipping = float(int(histogram[:6].sum()) / total * 100)
    highlight_clipping = float(int(histogram[250:].sum()) / total * 100)
    luminance_percentiles = [
        _histogram_percentile(cumulative, total, percent) for percent in (5, 25, 50, 75, 95)
    ]
    dark_pixel_percent = float(int(histogram[:64].sum()) / total * 100)
    bright_pixel_percent = float(int(histogram[225:].sum()) / total * 100)
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
        "luminance_percentiles": {
            "p05": round(float(luminance_percentiles[0]), 2),
            "p25": round(float(luminance_percentiles[1]), 2),
            "p50": round(float(luminance_percentiles[2]), 2),
            "p75": round(float(luminance_percentiles[3]), 2),
            "p95": round(float(luminance_percentiles[4]), 2),
        },
        "dark_pixel_percent": round(dark_pixel_percent, 3),
        "bright_pixel_percent": round(bright_pixel_percent, 3),
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
    metadata["maximum_input_edge"] = settings.AI_RESTORATION_MAX_INPUT_EDGE
    if min(width, height) > settings.AI_RESTORATION_MAX_INPUT_EDGE:
        metadata["fallback_reason"] = (
            "The source resolution is above the EDSR activation threshold; "
            "the original pixel grid was preserved."
        )
        return image, metadata
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


def _adapt_policy(base_policy, quality, level):
    policy = dict(base_policy)
    defects = quality["defects"]
    mean_luminance = quality["mean_luminance"]
    if mean_luminance > 175 or "overexposed_highlights" in defects:
        policy["exposure_mode"] = "darken"
        policy["gamma"] = {
            EnhancementLevel.LOW: 1.04,
            EnhancementLevel.MEDIUM: 1.08,
            EnhancementLevel.HIGH: 1.12,
        }[level]
        policy["blend"] = min(policy["blend"], {
            EnhancementLevel.LOW: 0.18,
            EnhancementLevel.MEDIUM: 0.26,
            EnhancementLevel.HIGH: 0.34,
        }[level])
        # CLAHE can brighten an already bright, nearly uniform image.  Gamma
        # correction alone is safer in this branch.
        policy["use_clahe"] = False
    elif mean_luminance >= 115:
        policy["exposure_mode"] = "balanced"
        policy["gamma"] = 1.0
        policy["blend"] = min(policy["blend"], {
            EnhancementLevel.LOW: 0.12,
            EnhancementLevel.MEDIUM: 0.20,
            EnhancementLevel.HIGH: 0.28,
        }[level])
        policy["use_clahe"] = True
    else:
        policy["exposure_mode"] = "brighten"
        policy["use_clahe"] = True
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


def _mean_chroma_shift(original, candidate):
    original_lab = cv2.cvtColor(original, cv2.COLOR_BGR2LAB).astype(np.float32)
    candidate_lab = cv2.cvtColor(candidate, cv2.COLOR_BGR2LAB).astype(np.float32)
    return float(np.mean(np.abs(candidate_lab[:, :, 1:] - original_lab[:, :, 1:])))


def _structural_similarity(original, candidate):
    """Return luminance SSIM on a bounded proxy without changing saved pixels."""
    if original.shape != candidate.shape:
        return 0.0
    height, width = original.shape[:2]
    longest_edge = max(height, width)
    if longest_edge > 512:
        scale = 512.0 / longest_edge
        dimensions = (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
        original = cv2.resize(original, dimensions, interpolation=cv2.INTER_AREA)
        candidate = cv2.resize(candidate, dimensions, interpolation=cv2.INTER_AREA)

    source = cv2.cvtColor(original, cv2.COLOR_BGR2GRAY).astype(np.float32)
    output = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY).astype(np.float32)
    constant_1 = (0.01 * 255) ** 2
    constant_2 = (0.03 * 255) ** 2
    source_mean = cv2.GaussianBlur(source, (11, 11), 1.5)
    output_mean = cv2.GaussianBlur(output, (11, 11), 1.5)
    source_variance = cv2.GaussianBlur(source * source, (11, 11), 1.5)
    source_variance -= source_mean * source_mean
    output_variance = cv2.GaussianBlur(output * output, (11, 11), 1.5)
    output_variance -= output_mean * output_mean
    covariance = cv2.GaussianBlur(source * output, (11, 11), 1.5)
    covariance -= source_mean * output_mean
    numerator = (
        (2 * source_mean * output_mean + constant_1)
        * (2 * covariance + constant_2)
    )
    denominator = (
        (source_mean * source_mean + output_mean * output_mean + constant_1)
        * (source_variance + output_variance + constant_2)
    )
    score = float(np.mean(numerator / np.maximum(denominator, 1e-6)))
    return float(np.clip(score, 0.0, 1.0))


def _face_restoration_metrics(original, candidate):
    absolute_change = np.abs(
        candidate.astype(np.float32) - original.astype(np.float32)
    )
    before = analyze_image_quality(original)
    after = analyze_image_quality(candidate)
    return {
        "structural_similarity": round(
            _structural_similarity(original, candidate),
            5,
        ),
        "mean_absolute_change": round(float(np.mean(absolute_change)), 3),
        "p95_absolute_change": round(
            float(np.percentile(absolute_change, 95)),
            3,
        ),
        "mean_chroma_shift": round(
            _mean_chroma_shift(original, candidate),
            3,
        ),
        "highlight_clipping_growth": round(
            after["highlight_clipping_percent"]
            - before["highlight_clipping_percent"],
            3,
        ),
    }


def _face_restoration_guard(original, candidate, configured_blend):
    """Accept only a bounded, identity-conservative restoration candidate."""
    if (
        not isinstance(candidate, np.ndarray)
        or candidate.shape != original.shape
        or candidate.dtype != np.uint8
        or not np.isfinite(candidate).all()
    ):
        raise GFPGANUnavailable(
            "The restored candidate did not preserve the source pixel grid."
        )

    limits = {
        "minimum_structural_similarity": float(
            settings.GFPGAN_MIN_STRUCTURAL_SIMILARITY
        ),
        "maximum_mean_absolute_change": float(
            settings.GFPGAN_MAX_MEAN_ABSOLUTE_CHANGE
        ),
        "maximum_p95_absolute_change": float(
            settings.GFPGAN_MAX_P95_ABSOLUTE_CHANGE
        ),
        "maximum_mean_chroma_shift": float(settings.GFPGAN_MAX_CHROMA_SHIFT),
        "maximum_highlight_clipping_growth": float(
            settings.GFPGAN_MAX_HIGHLIGHT_CLIPPING_GROWTH
        ),
    }
    minimum_blend = float(settings.GFPGAN_MIN_OUTPUT_BLEND)
    blend = float(np.clip(configured_blend, 0.0, 0.60))
    attempts = []
    while blend >= minimum_blend:
        blended = cv2.addWeighted(candidate, blend, original, 1.0 - blend, 0)
        metrics = _face_restoration_metrics(original, blended)
        violations = []
        if (
            metrics["structural_similarity"]
            < limits["minimum_structural_similarity"]
        ):
            violations.append("structural_similarity")
        if (
            metrics["mean_absolute_change"]
            > limits["maximum_mean_absolute_change"]
        ):
            violations.append("mean_absolute_change")
        if (
            metrics["p95_absolute_change"]
            > limits["maximum_p95_absolute_change"]
        ):
            violations.append("p95_absolute_change")
        if metrics["mean_chroma_shift"] > limits["maximum_mean_chroma_shift"]:
            violations.append("mean_chroma_shift")
        if (
            metrics["highlight_clipping_growth"]
            > limits["maximum_highlight_clipping_growth"]
        ):
            violations.append("highlight_clipping_growth")
        attempts.append(
            {
                "blend": round(blend, 4),
                "metrics": metrics,
                "violations": violations,
            }
        )
        if not violations:
            return blended, {
                "accepted": True,
                "configured_blend": round(configured_blend, 4),
                "applied_blend": round(blend, 4),
                "blend_reduced": blend < configured_blend,
                "limits": limits,
                "attempts": attempts,
            }
        blend *= 0.72

    return original, {
        "accepted": False,
        "configured_blend": round(configured_blend, 4),
        "applied_blend": 0.0,
        "blend_reduced": True,
        "limits": limits,
        "attempts": attempts,
        "rejection_reason": (
            "The restoration candidate exceeded identity or natural-image "
            "quality limits at the minimum safe blend."
        ),
    }


def _preserve_source_chroma(original, candidate):
    """Use learned luminance while retaining the source hue and skin colour."""
    original_lab = cv2.cvtColor(original, cv2.COLOR_BGR2LAB)
    candidate_lab = cv2.cvtColor(candidate, cv2.COLOR_BGR2LAB)
    protected_lab = cv2.merge(
        (candidate_lab[:, :, 0], original_lab[:, :, 1], original_lab[:, :, 2])
    )
    return cv2.cvtColor(protected_lab, cv2.COLOR_LAB2BGR)


def _amplify_source_chroma(source, protected, max_gain):
    """Restore some of the photo's own colour after a large brightening.

    ``protected`` has the learned luminance but the source's colour channels, so a
    photo brightened many times over looks grey. Each pixel's colour is scaled by
    the square root of how much its lightness grew (capped by ``max_gain``). The
    colour is smoothed first, since in near-black pixels it is mostly sensor noise,
    and only the source's own colour is used, never a model's, so no colour is
    invented. Returns ``(image, metadata)``.
    """
    metadata = {"applied": False, "max_gain": float(max_gain), "mean_gain": 1.0}
    if max_gain <= 1.0:
        return protected, metadata
    height, width = source.shape[:2]
    scale = min(1.0, 512.0 / max(height, width))
    small = (max(1, round(width * scale)), max(1, round(height * scale)))

    def to_small(channel):
        return cv2.resize(channel, small, interpolation=cv2.INTER_AREA)

    lab_source = cv2.cvtColor(source, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab_output = cv2.cvtColor(protected, cv2.COLOR_BGR2LAB).astype(np.float32)
    ratio = to_small(lab_output[:, :, 0]) / np.maximum(to_small(lab_source[:, :, 0]), 6.0)
    gain = cv2.GaussianBlur(np.sqrt(np.clip(ratio, 1.0, max_gain)), (0, 0), 3.5)
    gain = cv2.resize(gain, (width, height), interpolation=cv2.INTER_LINEAR)
    for channel in (1, 2):
        smooth = to_small(lab_source[:, :, channel])
        smooth = cv2.GaussianBlur(cv2.bilateralFilter(smooth, 0, 10, 6), (0, 0), 3.5)
        smooth = cv2.resize(smooth, (width, height), interpolation=cv2.INTER_LINEAR)
        lab_output[:, :, channel] = 128.0 + (smooth - 128.0) * gain
    result = cv2.cvtColor(np.clip(lab_output, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    metadata.update({"applied": True, "mean_gain": round(float(gain.mean()), 3)})
    return result, metadata


def _adaptive_learned_strength(input_quality, level):
    """Scale model strength by the actual need for additional exposure."""
    mean = input_quality["mean_luminance"] / 255.0
    median = input_quality["luminance_percentiles"]["p50"] / 255.0
    scene_luminance = (mean * 0.6) + (median * 0.4)
    threshold = settings.ENHANCEMENT_MODEL_LUMINANCE_THRESHOLD
    target = max(settings.ENHANCEMENT_MODEL_LUMINANCE_TARGET, threshold + 0.01)
    if input_quality["mean_luminance"] < settings.EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD:
        level_cap = settings.EXTREME_LOW_LIGHT_MODEL_STRENGTH
    else:
        level_cap = OUTPUT_GUARD_POLICIES[level]["learned_model_strength"]
    if scene_luminance >= threshold:
        return 0.0, scene_luminance
    need = np.clip((target - scene_luminance) / target, 0.0, 1.0)
    return float(level_cap * need), scene_luminance


def _quality_guard(original, candidate, level, input_quality, learned_model=False):
    """Apply a measurable, adaptive blend when a candidate becomes destructive."""
    limits = OUTPUT_GUARD_POLICIES[level]
    if input_quality["mean_luminance"] < settings.EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD:
        limits = {**limits, "max_luminance_gain": settings.EXTREME_LOW_LIGHT_MAX_LUMINANCE_GAIN}
    raw_quality = analyze_image_quality(candidate)
    raw_luminance_delta = (
        raw_quality["mean_luminance"] - input_quality["mean_luminance"]
    )
    raw_clipping_growth = (
        raw_quality["highlight_clipping_percent"]
        - input_quality["highlight_clipping_percent"]
    )
    scene_luminance = input_quality["mean_luminance"] / 255.0
    if learned_model:
        strength, scene_luminance = _adaptive_learned_strength(
            input_quality, level
        )
    else:
        strength = 1.0
    reasons = []

    if raw_luminance_delta > limits["max_luminance_gain"]:
        strength = min(strength, limits["max_luminance_gain"] / raw_luminance_delta)
        reasons.append("luminance_gain_limited")
    elif raw_luminance_delta < -limits["max_luminance_loss"]:
        strength = min(
            strength,
            limits["max_luminance_loss"] / abs(raw_luminance_delta),
        )
        reasons.append("luminance_loss_limited")
    if raw_clipping_growth > limits["max_highlight_clipping_growth"]:
        strength = min(
            strength,
            limits["max_highlight_clipping_growth"] / raw_clipping_growth,
        )
        reasons.append("highlight_clipping_limited")

    strength = max(0.0, min(1.0, strength))
    minimum_contrast = (
        input_quality["contrast_stddev"] * limits["minimum_contrast_ratio"]
    )
    guarded = original
    output_quality = input_quality
    chroma_shift = 0.0
    # Re-evaluate the real blended pixels rather than assuming that metrics are
    # linear. Reducing strength always moves the candidate toward the source.
    clipping_limit = settings.ENHANCEMENT_MAX_HIGHLIGHT_CLIPPING_PERCENT
    for _attempt in range(12):
        guarded = cv2.addWeighted(original, 1.0 - strength, candidate, strength, 0)
        output_quality = analyze_image_quality(guarded)
        chroma_shift = _mean_chroma_shift(original, guarded)
        contrast_ok = output_quality["contrast_stddev"] >= minimum_contrast
        clipping_growth = (
            output_quality["highlight_clipping_percent"]
            - input_quality["highlight_clipping_percent"]
        )
        clipping_ok = clipping_growth <= limits["max_highlight_clipping_growth"]
        absolute_clipping_ok = (
            output_quality["highlight_clipping_percent"] <= clipping_limit
        )
        chroma_ok = chroma_shift <= limits["max_chroma_shift"]
        if contrast_ok and clipping_ok and absolute_clipping_ok and chroma_ok:
            break
        if not contrast_ok and "contrast_preserved" not in reasons:
            reasons.append("contrast_preserved")
        if not clipping_ok and "highlight_clipping_limited" not in reasons:
            reasons.append("highlight_clipping_limited")
        if (
            not absolute_clipping_ok
            and "absolute_highlight_clipping_limited" not in reasons
        ):
            reasons.append("absolute_highlight_clipping_limited")
        if not chroma_ok and "chroma_shift_limited" not in reasons:
            reasons.append("chroma_shift_limited")
        strength *= 0.72

    luminance_delta = (
        output_quality["mean_luminance"] - input_quality["mean_luminance"]
    )
    clipping_growth = (
        output_quality["highlight_clipping_percent"]
        - input_quality["highlight_clipping_percent"]
    )
    if output_quality["highlight_clipping_percent"] > clipping_limit:
        logger.warning(
            "Enhancement output retained %.3f%% clipped highlights after guarding.",
            output_quality["highlight_clipping_percent"],
        )
    return guarded, output_quality, {
        "policy": "adaptive_natural_output_guard_v2",
        "fallback_blend_applied": strength < 0.999,
        "applied_candidate_strength": round(strength, 4),
        "reasons": reasons,
        "limits": limits,
        "scene_luminance_normalized": round(scene_luminance, 4),
        "model_luminance_threshold": settings.ENHANCEMENT_MODEL_LUMINANCE_THRESHOLD,
        "model_luminance_target": settings.ENHANCEMENT_MODEL_LUMINANCE_TARGET,
        "absolute_highlight_clipping_limit": clipping_limit,
        "raw_candidate": {
            "luminance_delta": round(raw_luminance_delta, 2),
            "contrast_stddev": raw_quality["contrast_stddev"],
            "highlight_clipping_growth": round(raw_clipping_growth, 3),
        },
        "luminance_gain": round(luminance_delta, 2),
        "contrast_retention_ratio": round(
            output_quality["contrast_stddev"]
            / max(input_quality["contrast_stddev"], 0.001),
            4,
        ),
        "mean_chroma_shift": round(chroma_shift, 3),
        "highlight_clipping_growth": round(clipping_growth, 3),
    }


def _is_near_grayscale(image):
    """Detect monochrome/near-monochrome source images for auto-colorization."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    chroma = np.abs(lab[:, :, 1] - 128.0) + np.abs(lab[:, :, 2] - 128.0)
    return float(np.mean(chroma)) <= settings.COLORIZATION_GRAYSCALE_CHROMA_THRESHOLD


def analyze_original_stage(context):
    """Validate the source, measure it once, and select the policy."""
    image = context.source_image
    if image is None or image.size == 0 or len(image.shape) != 3 or image.shape[2] != 3:
        raise ValidationError("The standardized image cannot be decoded safely.")
    if context.requested_level not in EnhancementLevel.values:
        raise ValidationError({"level": "Choose low, medium, high, or auto."})

    context.input_quality = analyze_image_quality(image)
    mean_luminance = context.input_quality["mean_luminance"]
    context.applied_level = (
        _auto_level(mean_luminance)
        if context.requested_level == EnhancementLevel.AUTO
        else context.requested_level
    )
    context.policy = _adapt_policy(
        LEVEL_POLICIES[context.applied_level],
        context.input_quality,
        context.applied_level,
    )
    context.zero_dce = {
        "requested": any(
            defect in context.input_quality["defects"]
            for defect in ("low_light", "severe_low_light")
        ),
        "applied": False,
        "fallback_reason": "",
    }
    context.colorization = {
        "requested": _is_near_grayscale(image),
        "applied": False,
        "fallback_reason": "",
    }
    return context


def exposure_enhancement_stage(context):
    """Apply learned exposure curves or the auditable OpenCV fallback."""
    source = context.source_image
    working = source
    if context.zero_dce["requested"] and settings.ZERO_DCE_ENABLED and context.use_zero_dce:
        try:
            working, zero_metadata = enhance_with_zero_dce(source)
            context.zero_dce.update(zero_metadata)
            context.zero_dce["applied"] = True
        except ZeroDCEUnavailable as error:
            context.zero_dce["fallback_reason"] = str(error)
    elif context.zero_dce["requested"]:
        context.zero_dce["fallback_reason"] = (
            "Zero-DCE is disabled." if not settings.ZERO_DCE_ENABLED
            else "Zero-DCE was switched off for this run."
        )

    if context.zero_dce["applied"]:
        protected = _preserve_source_chroma(source, working)
        context.current_image, gain = _amplify_source_chroma(
            source, protected, settings.ENHANCEMENT_DARK_COLOUR_GAIN_MAX,
        )
        context.zero_dce["chroma_protection"] = "preserve_source_lab_chroma"
        context.zero_dce["chroma_gain"] = gain
        return context

    policy = context.policy
    lab = cv2.cvtColor(source, cv2.COLOR_BGR2LAB)
    luminance, channel_a, channel_b = cv2.split(lab)
    corrected = cv2.LUT(luminance, _gamma_lut(policy["gamma"]))
    if policy["use_clahe"]:
        clahe = cv2.createCLAHE(
            clipLimit=policy["clahe_clip"],
            tileGridSize=(policy["clahe_grid"], policy["clahe_grid"]),
        )
        corrected = clahe.apply(corrected)
    corrected = cv2.addWeighted(
        luminance,
        1.0 - policy["blend"],
        corrected,
        policy["blend"],
        0,
    )
    context.current_image = cv2.cvtColor(
        cv2.merge((corrected, channel_a, channel_b)),
        cv2.COLOR_LAB2BGR,
    )
    return context


def output_guard_a_stage(context):
    """Guard exposure output before any noise or detail processing."""
    guarded, output_quality, metadata = _quality_guard(
        context.source_image,
        context.current_image,
        context.applied_level,
        context.input_quality,
        learned_model=context.zero_dce["applied"],
    )
    metadata["stage"] = "post_exposure"
    context.current_image = guarded
    context.output_quality = output_quality
    context.quality_guards["guard_a"] = metadata
    return context


def measure_post_exposure_noise_stage(context):
    """Measure revealed noise only after safe exposure correction."""
    quality = analyze_image_quality(context.current_image)
    context.post_exposure_noise = {
        "measurement_point": "after_guard_a",
        "estimated_noise": quality["estimated_noise"],
        "visible_noise": "visible_noise" in quality["defects"],
        "quality": quality,
    }
    return context


def _adaptive_denoise_strength(noise_score):
    breakpoints = tuple(settings.ADAPTIVE_DENOISE_BREAKPOINTS)
    strengths = tuple(settings.ADAPTIVE_DENOISE_STRENGTHS)
    if len(strengths) != len(breakpoints) + 1:
        raise RuntimeError(
            "ADAPTIVE_DENOISE_STRENGTHS must contain exactly one more "
            "value than ADAPTIVE_DENOISE_BREAKPOINTS."
        )
    if tuple(sorted(breakpoints)) != breakpoints:
        raise RuntimeError("ADAPTIVE_DENOISE_BREAKPOINTS must be ascending.")
    return int(strengths[bisect_right(breakpoints, float(noise_score))])


def _gradient_edge_strength(image):
    """Normalized 0-1 gradient magnitude: ~1 at edges, ~0 in flat regions."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.GaussianBlur(cv2.magnitude(gradient_x, gradient_y), (0, 0), 3.0)
    edge_reference = float(np.percentile(gradient, 75)) + 1e-6
    return np.clip(gradient / edge_reference, 0.0, 1.0)


def _edge_aware_denoise(image, strength):
    """Blend a strong NLM candidate into flat regions only.

    Flat, low-gradient regions (sky, walls, canopy interior) get the full
    denoise strength. High-gradient regions (faces, edges, text, vehicle
    trim) keep the sharp source, since a single global NLM strength either
    leaves grain in flat areas or smears real detail once it is strong
    enough to clear that grain.
    """
    strong = cv2.fastNlMeansDenoisingColored(image, None, strength, strength, 7, 21)
    # Find edges on the denoised candidate. On the noisy input, grain itself
    # produces gradients everywhere, so almost nothing looked flat and most of
    # the noise survived at any strength (a brightened night photo kept ~80% of
    # its grain). Real edges are still there once the grain is gone.
    edge_strength = _gradient_edge_strength(strong)
    denoise_weight = (1.0 - edge_strength) ** 2.0
    denoise_weight = cv2.merge([denoise_weight] * 3).astype(np.float32)
    blended = (
        image.astype(np.float32) * (1.0 - denoise_weight)
        + strong.astype(np.float32) * denoise_weight
    )
    return np.clip(blended, 0, 255).astype(np.uint8)


def adaptive_denoise_stage(context):
    """Denoise measured output noise without inventing facial detail."""
    enabled = bool(settings.ADAPTIVE_DENOISE_ENABLED) or context.style == EnhancementStyle.AI_MAX
    noise_score = context.post_exposure_noise["estimated_noise"]
    strength = 0
    reason = "not_required"

    if enabled and context.applied_level in {
        EnhancementLevel.MEDIUM,
        EnhancementLevel.HIGH,
    }:
        strength = _adaptive_denoise_strength(noise_score)
        reason = "post_exposure_noise_mapping"
    elif enabled:
        reason = "low_level_preserves_source_texture"
    else:
        # Preserve the legacy, input-defect-driven behaviour while the new
        # rollout flag remains disabled.
        strength = int(context.policy.get("denoise", 0))
        reason = "feature_flag_disabled_legacy_policy"

    # Judge blur on the brightened image. A near-black frame always measures as
    # "severe blur" (almost no Laplacian variance), which used to switch denoising
    # off for exactly the night photos that come out grainiest.
    if "severe_blur" in context.post_exposure_noise["quality"]["defects"]:
        strength = 0
        reason = "severe_blur_protection"

    applied = strength > 0
    if applied:
        context.current_image = _edge_aware_denoise(context.current_image, strength)
    context.adaptive_denoise = {
        "enabled": enabled,
        "applied": applied,
        "strength": strength,
        "reason": reason,
        "method": "edge_aware_nlm_blend",
        "input_noise_score": noise_score,
        "measurement_point": "after_guard_a",
        "breakpoints": list(settings.ADAPTIVE_DENOISE_BREAKPOINTS),
        "strengths": list(settings.ADAPTIVE_DENOISE_STRENGTHS),
    }
    return context


def purevision_restoration_stage(context):
    """Optionally remove compression artifacts with PureVision (1x, no resize).

    PureVision never changes the pixel grid, so it is safe to run ahead of
    the resolution-changing stages below. It is an independently gated,
    third-party (CC BY-NC-SA 4.0) alternative to NAFNet and defaults to
    disabled until visual QA and a license review clear it for the intended
    deployment.
    """
    metadata = {
        "enabled": bool(settings.PUREVISION_ENABLED),
        "requested": bool(settings.PUREVISION_ENABLED),
        "applied": False,
        "fallback_reason": "",
    }
    if not settings.PUREVISION_ENABLED:
        metadata["fallback_reason"] = "PureVision is disabled."
        context.purevision = metadata
        return context
    if context.input_quality["mean_luminance"] < settings.EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD:
        metadata["fallback_reason"] = (
            "The source was too dark for PureVision; its residual sensor "
            "noise is out of distribution for this model and produces "
            "hallucinated texture instead of a clean result."
        )
        context.purevision = metadata
        return context
    try:
        restored, provider_metadata = restore_with_purevision(context.current_image)
        metadata.update(provider_metadata)
        metadata["applied"] = True
        context.current_image = restored
    except PureVisionUnavailable as error:
        metadata["fallback_reason"] = str(error)
        context.warnings.append(str(error))
    context.purevision = metadata
    return context


def low_resolution_restoration_stage(context):
    """Run EDSR only after denoise and only if resolution is still low."""
    quality_at_stage = analyze_image_quality(context.current_image)
    if settings.HD_RESTORATION_ENABLED:
        restored = context.current_image
        metadata = {
            "requested": False,
            "applied": False,
            "provider": "opencv_dnn_edsr",
            "scale": settings.AI_RESTORATION_SCALE,
            "fallback_reason": "Superseded by the enabled NAFNet/Real-ESRGAN HD pipeline.",
        }
    else:
        restored, metadata = restore_low_resolution(
            context.current_image,
            quality_at_stage,
        )
    metadata["stage"] = "after_adaptive_denoise"
    metadata["quality_at_stage"] = quality_at_stage
    context.current_image = restored
    context.restoration = metadata
    # Guard B protects subsequent sharpening (and future restoration stages)
    # against this safe, resolution-normalized reference.
    context.final_guard_reference = restored.copy()
    return context


def hd_restoration_stage(context):
    """Optionally deblur with NAFNet and create a 2x/4x HD display output."""
    metadata = {
        "enabled": bool(settings.HD_RESTORATION_ENABLED),
        "requested": bool(settings.HD_RESTORATION_ENABLED),
        "applied": False,
        "deblur": {
            "enabled": bool(settings.NAFNET_DEBLUR_ENABLED),
            "requested": bool(settings.HD_RESTORATION_ENABLED and settings.NAFNET_DEBLUR_ENABLED),
            "applied": False,
        },
        "super_resolution": {"requested": bool(settings.HD_RESTORATION_ENABLED), "applied": False},
        "fallback_reason": "",
        "recognition_policy": "display_output_only; recognition_uses_original",
    }
    if not settings.HD_RESTORATION_ENABLED:
        metadata["fallback_reason"] = "HD restoration is disabled."
        context.hd_restoration = metadata
        return context
    source_dimensions = [int(context.current_image.shape[1]), int(context.current_image.shape[0])]
    working = context.current_image
    if settings.NAFNET_DEBLUR_ENABLED:
        try:
            deblurred, deblur_metadata = deblur_with_nafnet(context.current_image)
            metadata["deblur"].update(deblur_metadata)
            metadata["deblur"]["applied"] = True
            working = deblurred
        except HDRestorationUnavailable as error:
            metadata["deblur"]["fallback_reason"] = str(error)
            context.warnings.append(str(error))
    else:
        metadata["deblur"]["fallback_reason"] = (
            "NAFNet deblurring is disabled pending checkpoint visual QA."
        )
    try:
        upscaled, upscale_metadata = upscale_with_realesrgan(working)
        blend = min(max(float(settings.REALESRGAN_OUTPUT_BLEND), 0.0), 1.0)
        reference_hd = cv2.resize(
            working,
            (upscaled.shape[1], upscaled.shape[0]),
            interpolation=cv2.INTER_CUBIC,
        )
        upscaled = cv2.addWeighted(upscaled, blend, reference_hd, 1.0 - blend, 0)
        metadata["super_resolution"].update(upscale_metadata)
        metadata["super_resolution"]["applied"] = True
        metadata["super_resolution"]["candidate_blend"] = blend
        metadata["super_resolution"]["source_grid_protection"] = (
            "bicubic_reference_blend"
        )
        metadata["applied"] = True
        metadata["input_dimensions"] = source_dimensions
        metadata["output_dimensions"] = [int(upscaled.shape[1]), int(upscaled.shape[0])]
        context.current_image = upscaled
        context.final_guard_reference = upscaled.copy()
    except HDRestorationUnavailable as error:
        metadata["fallback_reason"] = str(error)
        context.warnings.append(str(error))
    context.hd_restoration = metadata
    return context


def purescale_super_resolution_stage(context):
    """Optionally upscale 2x with PureScale as a Real-ESRGAN alternative.

    PureScale must not stack with ``hd_restoration_stage``'s Real-ESRGAN
    pass, since two independent upscalers would compound scale factors and
    could exceed the output-pixel safety limit. It only runs when HD
    restoration did not already produce a super-resolution candidate. It is
    an independently gated, third-party (CC BY-NC-SA 4.0) checkpoint and
    defaults to disabled until visual QA and a license review clear it for
    the intended deployment.
    """
    metadata = {
        "enabled": bool(settings.PURESCALE_ENABLED),
        "requested": bool(settings.PURESCALE_ENABLED and not context.hd_restoration.get("applied")),
        "applied": False,
        "fallback_reason": "",
    }
    if not settings.PURESCALE_ENABLED:
        metadata["fallback_reason"] = "PureScale is disabled."
        context.purescale = metadata
        return context
    if context.hd_restoration.get("applied"):
        metadata["fallback_reason"] = "HD restoration already produced a super-resolution candidate."
        context.purescale = metadata
        return context
    if context.input_quality["mean_luminance"] < settings.EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD:
        metadata["fallback_reason"] = (
            "The source was too dark for PureScale; upscaling residual "
            "sensor noise or PureVision artifacts would only make them "
            "more visible."
        )
        context.purescale = metadata
        return context
    source_dimensions = [int(context.current_image.shape[1]), int(context.current_image.shape[0])]
    try:
        upscaled, provider_metadata = upscale_with_purescale(context.current_image)
        metadata.update(provider_metadata)
        metadata["applied"] = True
        metadata["input_dimensions"] = source_dimensions
        metadata["output_dimensions"] = [int(upscaled.shape[1]), int(upscaled.shape[0])]
        context.current_image = upscaled
        context.final_guard_reference = upscaled.copy()
    except PureScaleUnavailable as error:
        metadata["fallback_reason"] = str(error)
        context.warnings.append(str(error))
    context.purescale = metadata
    return context


def restore_degraded_faces(image, input_quality):
    """Build a conservative GFPGAN candidate for genuinely degraded faces."""
    eligible_defect_names = {"severe_blur", "low_resolution"}
    if settings.GFPGAN_ALLOW_MILD_BLUR:
        eligible_defect_names.add("mild_blur")
    eligible_defects = sorted(
        set(input_quality["defects"])
        & eligible_defect_names
    )
    metadata = {
        "requested": bool(eligible_defects),
        "eligible_defects": eligible_defects,
        "enabled": bool(settings.GFPGAN_ENABLED),
        "applied": False,
        "provider": "private_gfpgan_v1_4",
        "fallback_reason": "",
        "identity_safeguard": (
            "bounded_change_quality_guard_not_identity_verification"
        ),
        "identity_embedding_verified": False,
    }
    if not metadata["requested"]:
        metadata["fallback_reason"] = "Face restoration is not required."
        return image, metadata
    if not settings.GFPGAN_ENABLED:
        metadata["fallback_reason"] = "Private face restoration is disabled."
        return image, metadata

    blend = min(max(float(settings.GFPGAN_OUTPUT_BLEND), 0.0), 0.60)
    try:
        candidate, provider_metadata = restore_faces(image)
        blended, guard = _face_restoration_guard(image, candidate, blend)
        metadata.update(provider_metadata)
        metadata["candidate_guard"] = guard
        if not guard["accepted"]:
            metadata["fallback_reason"] = guard["rejection_reason"]
            return image, metadata
        metadata.update(
            {
                "applied": True,
                "output_blend": guard["applied_blend"],
                "input_dimensions": [int(image.shape[1]), int(image.shape[0])],
                "output_dimensions": [
                    int(blended.shape[1]),
                    int(blended.shape[0]),
                ],
                "quality_before": analyze_image_quality(image),
                "quality_after_candidate_blend": analyze_image_quality(blended),
            }
        )
        return blended, metadata
    except GFPGANUnavailable as error:
        metadata["fallback_reason"] = str(error)
        return image, metadata
    except (RuntimeError, ValueError, TypeError, cv2.error, OSError):
        logger.exception("GFPGAN candidate validation failed safely.")
        metadata["fallback_reason"] = (
            "The face-restoration candidate failed validation safely."
        )
        return image, metadata


def face_restoration_stage(context):
    """Restore only eligible degraded faces; keep the background unchanged."""
    restored, metadata = restore_degraded_faces(
        context.current_image,
        context.input_quality,
    )
    metadata["stage"] = "after_low_resolution_restoration"
    context.current_image = restored
    context.face_restoration = metadata
    return context


def sharpen_stage(context):
    """Apply only the bounded policy sharpening after optional restoration.

    Sharpening is weighted by local gradient strength so it cannot
    re-amplify the quantization steps left behind in regions the denoise
    stage just smoothed (plain unsharp masking treats those steps as edges
    and turns them back into visible speckle).
    """
    strength = context.policy["sharpen"]
    if strength:
        image = context.current_image
        blurred = cv2.GaussianBlur(image, (0, 0), 1.0)
        detail = image.astype(np.float32) - blurred.astype(np.float32)
        edge_strength = _gradient_edge_strength(image)
        edge_strength = cv2.merge([edge_strength] * 3).astype(np.float32)
        sharpened = image.astype(np.float32) + detail * strength * edge_strength
        context.current_image = np.clip(sharpened, 0, 255).astype(np.uint8)
    return context


def output_guard_b_stage(context):
    """Apply the final safety guard after denoise, EDSR, and sharpening."""
    reference = context.final_guard_reference
    reference_quality = analyze_image_quality(reference)
    guarded, output_quality, metadata = _quality_guard(
        reference,
        context.current_image,
        context.applied_level,
        reference_quality,
        learned_model=False,
    )
    metadata["stage"] = "final_output"
    context.current_image = guarded
    context.output_quality = output_quality
    context.quality_guards["guard_b"] = metadata
    return context


def validate_output_stage(context):
    """Enforce the final pixel-grid and datatype contract before persistence.

    Runs last, after every other stage (including the identity guard), so it
    judges the pixels that will actually be saved.
    """
    output = context.current_image
    if (
        not isinstance(output, np.ndarray)
        or output.ndim != 3
        or output.shape[2] != 3
        or output.size == 0
        or output.dtype != np.uint8
        or not np.isfinite(output).all()
    ):
        raise ValidationError("The enhanced output failed final image validation.")

    source_dimensions = [
        int(context.source_image.shape[1]),
        int(context.source_image.shape[0]),
    ]
    expected_dimensions = source_dimensions
    source = "original_pixel_grid"
    # At most one of these ever changes the pixel grid: hd_restoration_stage
    # and purescale_super_resolution_stage are mutually exclusive (see their
    # own docstrings), and low_resolution_restoration_stage (EDSR) only runs
    # when HD restoration is disabled. PureVision never resizes.
    if context.hd_restoration.get("applied"):
        expected_dimensions = list(context.hd_restoration["output_dimensions"])
        source = "bounded_hd_restoration_output_grid"
    elif context.purescale.get("applied"):
        expected_dimensions = list(context.purescale["output_dimensions"])
        source = "bounded_purescale_output_grid"
    elif context.restoration.get("applied"):
        expected_dimensions = list(context.restoration["output_dimensions"])
        source = "bounded_edsr_output_grid"
    actual_dimensions = [int(output.shape[1]), int(output.shape[0])]
    if actual_dimensions != expected_dimensions:
        raise ValidationError(
            "The enhanced output violated the configured resolution contract."
        )

    context.resolution_contract = {
        "validated": True,
        "source_dimensions": source_dimensions,
        "expected_output_dimensions": expected_dimensions,
        "actual_output_dimensions": actual_dimensions,
        "pixel_grid_source": source,
        "full_resolution_inference": bool(
            settings.ENHANCEMENT_PRESERVE_ORIGINAL_RESOLUTION
        ),
        "enhanced_image_was_not_downscaled": (
            actual_dimensions[0] >= source_dimensions[0]
            and actual_dimensions[1] >= source_dimensions[1]
        ),
    }
    return context


def colorization_stage(context):
    """Optionally colorize a detected grayscale source, as the final stage.

    Colorization deliberately introduces large chroma changes, which would
    be undone by every earlier chroma-preservation/chroma-shift guard in
    this pipeline (they exist to keep hue close to the source). Running this
    last, after output_guard_b_stage, means no downstream guard ever sees
    or fights the new colour.
    """
    metadata = {
        "enabled": bool(settings.COLORIZATION_ENABLED),
        "requested": context.colorization["requested"],
        "applied": False,
        "fallback_reason": "",
    }
    if not settings.COLORIZATION_ENABLED:
        metadata["fallback_reason"] = "Colorization is disabled."
        context.colorization = metadata
        return context
    if not context.colorization["requested"]:
        metadata["fallback_reason"] = "The source image already has colour."
        context.colorization = metadata
        return context
    try:
        colorized, provider_metadata = colorize_with_siggraph17(context.current_image)
        metadata.update(provider_metadata)
        metadata["applied"] = True
        context.current_image = colorized
    except ColorizationUnavailable as error:
        metadata["fallback_reason"] = str(error)
        context.warnings.append(str(error))
    context.colorization = metadata
    return context


def dynamic_style_stage(context):
    """Apply the bounded vivid preset when the "dynamic" style was requested."""
    if context.style != EnhancementStyle.DYNAMIC:
        context.dynamic_style = {"applied": False}
        return context
    vivid, vivid_metadata = apply_gallery_adjustments(
        context.current_image,
        EditMode.MANUAL,
        {"contrast": 18, "saturation": 24, "warmth": 4},
    )
    context.current_image = vivid
    context.dynamic_style = {
        "applied": True,
        "policy": "bounded_vivid_preset",
        "details": vivid_metadata,
    }
    return context


def identity_guard_stage(context):
    """Blend back toward the source when enhancement changed who a face looks like.

    Runs last so it judges the final image, whichever stages produced it.
    """
    metadata = {
        "enabled": bool(settings.IDENTITY_GUARD_ENABLED),
        "requested": False,
        "applied": False,
        "status": "disabled",
        "fallback_reason": "",
    }
    if not settings.IDENTITY_GUARD_ENABLED:
        metadata["fallback_reason"] = "The identity guard is disabled."
        context.identity_guard = metadata
        return context
    if not context.allow_biometric_processing:
        metadata["status"] = "skipped"
        metadata["fallback_reason"] = "Biometric consent has not been granted."
        context.identity_guard = metadata
        return context
    try:
        guarded, guard_metadata = apply_identity_guard(
            context.source_image, context.current_image
        )
        metadata.update(guard_metadata)
        context.current_image = guarded
        if guard_metadata["status"] == "unresolved":
            context.warnings.append(
                "Enhancement changed a face more than the identity guard allows "
                "even at the most conservative strength."
            )
    except IdentityGuardUnavailable as error:
        metadata["status"] = "skipped"
        metadata["fallback_reason"] = str(error)
    except (cv2.error, ValueError, TypeError):
        logger.exception("The identity guard failed safely.")
        metadata["status"] = "skipped"
        metadata["fallback_reason"] = "The identity guard failed safely."
    context.identity_guard = metadata
    return context


def enhance_bgr(
    image, requested_level=EnhancementLevel.AUTO, style=EnhancementStyle.NATURAL,
    allow_biometric_processing=True, use_zero_dce=True,
):
    """Return an enhanced BGR image, applied level, and auditable parameters.

    ``style`` is a separate axis from ``requested_level`` (which controls
    exposure-correction strength): "natural" is the default pipeline
    unchanged, "dynamic" adds a bounded, guarded vivid contrast/saturation
    boost as a final step, and "ai_max" forces HIGH level and enables
    adaptive denoise for this call regardless of the platform default (it
    does not bypass the separate PURESCALE_ENABLED/PUREVISION_ENABLED/
    GFPGAN_ENABLED license-gated flags -- those stay under the platform
    operator's control either way).
    """
    if style == EnhancementStyle.AI_MAX:
        requested_level = EnhancementLevel.HIGH
    context = EnhancementPipelineContext(
        image, requested_level, style=style,
        allow_biometric_processing=allow_biometric_processing,
        use_zero_dce=use_zero_dce,
    )
    stages = (
        ("analyze_original", analyze_original_stage),
        ("exposure_enhancement", exposure_enhancement_stage),
        ("output_guard_a", output_guard_a_stage),
        ("measure_post_exposure_noise", measure_post_exposure_noise_stage),
        ("adaptive_denoise", adaptive_denoise_stage),
        ("purevision_restoration", purevision_restoration_stage),
        ("low_resolution_restoration", low_resolution_restoration_stage),
        ("hd_restoration", hd_restoration_stage),
        ("purescale_super_resolution", purescale_super_resolution_stage),
        ("face_restoration", face_restoration_stage),
        ("sharpen", sharpen_stage),
        ("output_guard_b", output_guard_b_stage),
        ("colorization", colorization_stage),
        ("dynamic_style", dynamic_style_stage),
        ("identity_guard", identity_guard_stage),
        ("validate_output", validate_output_stage),
    )
    for stage_name, stage_function in stages:
        with _timed_pipeline_stage(context, stage_name):
            stage_function(context)

    normalized_comparison = compare_image_quality(
        context.source_image,
        context.current_image,
    )
    if context.zero_dce["requested"] and not context.zero_dce["applied"]:
        context.warnings.append(
            "Zero-DCE was requested for low light but was unavailable; "
            "the adaptive OpenCV fallback was used."
        )
    if (
        context.input_quality["needs_ai_restoration"]
        and not context.restoration["applied"]
        and not context.face_restoration["applied"]
    ):
        context.warnings.append(
            "Severe blur or low resolution requires an AI restoration model; "
            "the baseline avoids inventing facial detail."
        )

    algorithm = (
        "zero_dce_hybrid_v1"
        if context.zero_dce["applied"]
        else "opencv_adaptive_restoration_router_v2"
    )
    if context.face_restoration["applied"]:
        algorithm = f"{algorithm}+private_gfpgan_v1_4"
    if context.purevision["applied"]:
        algorithm = f"{algorithm}+purevision_rrdb_1x"
    if context.hd_restoration["applied"]:
        if context.hd_restoration["deblur"]["applied"]:
            algorithm = f"{algorithm}+nafnet_gopro"
        algorithm = f"{algorithm}+realesrgan_x4plus"
    if context.purescale["applied"]:
        algorithm = f"{algorithm}+purescale_safmn_2x"
    if context.colorization["applied"]:
        algorithm = f"{algorithm}+colorization_siggraph17"
    if context.dynamic_style["applied"]:
        algorithm = f"{algorithm}+dynamic_vivid_boost"
    if context.identity_guard.get("applied"):
        algorithm = f"{algorithm}+identity_guard"

    parameters = {
        **context.policy,
        "style": context.style,
        "dynamic_style": context.dynamic_style,
        "identity_guard": context.identity_guard,
        "mean_input_luminance": context.input_quality["mean_luminance"],
        "algorithm": algorithm,
        "pipeline_version": "staged_natural_enhancement_v3",
        "colour_policy": (
            "learned_rgb_curves"
            if context.zero_dce["applied"]
            else "preserve_lab_chroma_channels"
        ),
        "quality_before": context.input_quality,
        "quality_after": context.output_quality,
        "quality_comparison": normalized_comparison,
        # Backward-compatible alias retained for clients that previously
        # consumed the single exposure guard. The complete two-guard contract
        # is available in ``quality_guards``.
        "quality_guard": context.quality_guards["guard_a"],
        "quality_guards": context.quality_guards,
        "post_exposure_noise": context.post_exposure_noise,
        "adaptive_denoise": context.adaptive_denoise,
        "resolution_contract": context.resolution_contract,
        "restoration": context.restoration,
        "hd_restoration": context.hd_restoration,
        "purevision": context.purevision,
        "purescale": context.purescale,
        "face_restoration": context.face_restoration,
        "colorization": context.colorization,
        "zero_dce": context.zero_dce,
        "stage_order": context.stage_order,
        "stage_timings_ms": context.stage_timings_ms,
        "feature_flags": {
            "adaptive_denoise_enabled": settings.ADAPTIVE_DENOISE_ENABLED,
            "gfpgan_enabled": settings.GFPGAN_ENABLED,
            "gfpgan_queue_enabled": settings.GFPGAN_QUEUE_ENABLED,
            "hd_restoration_enabled": settings.HD_RESTORATION_ENABLED,
            "nafnet_deblur_enabled": settings.NAFNET_DEBLUR_ENABLED,
            "purevision_enabled": settings.PUREVISION_ENABLED,
            "purescale_enabled": settings.PURESCALE_ENABLED,
            "colorization_enabled": settings.COLORIZATION_ENABLED,
            "identity_guard_enabled": settings.IDENTITY_GUARD_ENABLED,
        },
        "warnings": context.warnings,
    }
    return context.current_image, context.applied_level, parameters


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


def _read_full_resolution_original(result):
    """Decode the EXIF-oriented original without a resize or destructive overwrite."""
    original = result.image.original_file
    if not original:
        raise ValidationError("The original image is unavailable.")
    original.open("rb")
    try:
        with Image.open(original) as source:
            source.load()
            working = ImageOps.exif_transpose(source)
            if working.mode in {"RGBA", "LA"} or (
                working.mode == "P" and "transparency" in working.info
            ):
                rgba = working.convert("RGBA")
                background = Image.new(
                    "RGB",
                    rgba.size,
                    ImageColor.getrgb(settings.PREPROCESS_TRANSPARENT_BACKGROUND),
                )
                background.paste(rgba, mask=rgba.getchannel("A"))
                working = background
            else:
                working = working.convert("RGB")
            rgb = np.asarray(working, dtype=np.uint8)
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValidationError("The original image cannot be decoded safely.") from error
    finally:
        original.close()
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _read_enhancement_source(result):
    if settings.ENHANCEMENT_PRESERVE_ORIGINAL_RESOLUTION:
        try:
            return _read_full_resolution_original(result), "full_resolution_original"
        except ValidationError:
            # Legacy rows can legitimately have only their standardized file.
            pass
    return _read_standardized_image(result.preprocessing_result), "standardized_fallback"


def _encode_output(image, preprocessing):
    use_png = preprocessing.output_format == "png"
    extension = ".png" if use_png else ".jpg"
    output_format = "png" if use_png else "jpeg"
    options = (
        [cv2.IMWRITE_PNG_COMPRESSION, 6]
        if use_png
        else [cv2.IMWRITE_JPEG_QUALITY, settings.PREPROCESS_JPEG_QUALITY]
    )
    if not use_png and hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR") and hasattr(
        cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444"
    ):
        options.extend(
            [
                cv2.IMWRITE_JPEG_SAMPLING_FACTOR,
                cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,
            ]
        )
    success, encoded = cv2.imencode(extension, image, options)
    if not success:
        raise ValidationError("The enhanced image could not be encoded safely.")
    return encoded.tobytes(), extension, output_format


class TooManyActiveJobs(APIException):
    status_code = http_status.HTTP_429_TOO_MANY_REQUESTS
    default_code = "too_many_active_jobs"
    default_detail = "Too many enhancements are already in progress. Wait for some to finish."


ACTIVE_STATUSES = (
    EnhancementStatus.PENDING, EnhancementStatus.QUEUED, EnhancementStatus.PROCESSING,
)


def ensure_enhancement_capacity(owner, *, batch):
    """Refuse new work once the owner has too much in flight (fair use).

    Single-image requests and batch jobs are limited separately: a batch fans out
    into one result per image, so counting those against the single-image limit
    would make every batch of more than a handful of images impossible.
    """
    if batch:
        limit = settings.MAX_ACTIVE_BATCH_ENHANCEMENTS_PER_USER
        active = EnhancementBatchJob.objects.filter(
            owner=owner, status__in=ACTIVE_STATUSES,
        ).count()
        noun = "batch enhancement jobs"
    else:
        limit = settings.MAX_ACTIVE_ENHANCEMENTS_PER_USER
        active = ImageEnhancementResult.objects.filter(
            owner=owner, batch_job__isnull=True, status__in=ACTIVE_STATUSES,
        ).count()
        noun = "image enhancements"
    if limit and active >= limit:
        raise TooManyActiveJobs(
            f"You already have {active} {noun} in progress (limit {limit}). "
            "Wait for some to finish, then try again."
        )


def create_enhancement_result(
    image, requested_level=EnhancementLevel.AUTO, batch_job=None,
    requested_style=EnhancementStyle.NATURAL,
):
    if image.owner_id != (batch_job.owner_id if batch_job else image.owner_id):
        raise ValidationError("The enhancement owner does not match the image owner.")
    try:
        preprocessing = image.preprocessing_result
    except ImagePreprocessingResult.DoesNotExist as error:
        raise ValidationError("The image must finish preprocessing before enhancement.") from error
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("The image must finish preprocessing before enhancement.")
    return ImageEnhancementResult.objects.create(
        owner=image.owner,
        image=image,
        preprocessing_result=preprocessing,
        batch_job=batch_job,
        requested_level=requested_level,
        requested_style=requested_style,
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
        source, source_strategy = _read_enhancement_source(result)
        from privacy.services import biometric_processing_allowed
        enhanced, applied_level, parameters = enhance_bgr(
            source, result.requested_level, style=result.requested_style,
            allow_biometric_processing=biometric_processing_allowed(result.owner),
        )
        parameters["source_resolution_strategy"] = source_strategy
        parameters["source_dimensions"] = [
            int(source.shape[1]), int(source.shape[0])
        ]
        try:
            quality = result.image.quality_decision
        except ImageQualityDecision.DoesNotExist:
            quality = None
        quality_gate = {"decision": quality.decision if quality else None, "bypassed": False}
        if quality and quality.status == "completed" and quality.decision != "accepted":
            quality_gate["bypassed"] = True
            quality_gate["defects"] = quality.defects
            quality_gate["rejection_reasons"] = quality.rejection_reasons
            parameters["warnings"].append(
                "The source image failed quality checks "
                f"({', '.join(quality.rejection_reasons or quality.defects or ['unspecified'])}); "
                "enhancement was still attempted and may not fully correct the source defects."
            )
        parameters["quality_gate"] = quality_gate
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


EDITOR_CONTROL_NAMES = (
    "brightness",
    "contrast",
    "shadows",
    "saturation",
    "highlights",
    "warmth",
)


def _auto_editor_adjustments(quality):
    """Create restrained corrections from the image rather than a fixed preset."""
    percentiles = quality["luminance_percentiles"]
    brightness = np.clip((135.0 - quality["mean_luminance"]) * 0.22, -16, 16)
    contrast = np.clip((48.0 - quality["contrast_stddev"]) * 0.65, -8, 20)
    shadows = np.clip((68.0 - percentiles["p25"]) * 0.35, 0, 22)
    highlights = -np.clip((percentiles["p95"] - 224.0) * 0.45, 0, 24)
    return {
        "brightness": round(float(brightness), 2),
        "contrast": round(float(contrast), 2),
        "shadows": round(float(shadows), 2),
        "saturation": 0.0,
        "highlights": round(float(highlights), 2),
        "warmth": 0.0,
    }


def _render_editor_controls(source, controls, strength=1.0):
    """Apply bounded tone controls in LAB while preserving the source pixel grid."""
    lab = cv2.cvtColor(source, cv2.COLOR_BGR2LAB).astype(np.float32)
    luminance, channel_a, channel_b = cv2.split(lab)
    effective = {
        name: float(controls.get(name, 0.0)) * strength
        for name in EDITOR_CONTROL_NAMES
    }

    luminance += effective["brightness"] * 0.35
    midpoint = float(np.median(luminance))
    contrast_factor = 1.0 + (effective["contrast"] * 0.005)
    luminance = midpoint + ((luminance - midpoint) * contrast_factor)

    normalized = np.clip(luminance / 255.0, 0.0, 1.0)
    shadow_mask = np.square(1.0 - normalized)
    highlight_mask = np.square(normalized)
    luminance += shadow_mask * effective["shadows"] * 0.45
    luminance += highlight_mask * effective["highlights"] * 0.40

    saturation_factor = 1.0 + (effective["saturation"] * 0.004)
    channel_a = 128.0 + ((channel_a - 128.0) * saturation_factor)
    channel_b = 128.0 + ((channel_b - 128.0) * saturation_factor)
    # Warmth shifts the blue-yellow axis: positive is warmer (more yellow).
    channel_b += effective["warmth"] * 0.30
    rendered_lab = cv2.merge(
        (
            np.clip(luminance, 0, 255).astype(np.uint8),
            np.clip(channel_a, 0, 255).astype(np.uint8),
            np.clip(channel_b, 0, 255).astype(np.uint8),
        )
    )
    return cv2.cvtColor(rendered_lab, cv2.COLOR_LAB2BGR), effective


def apply_gallery_adjustments(source, mode, requested_adjustments=None, baseline=None):
    """Render one non-destructive Auto or Manual gallery edit.

    ``baseline`` is the pre-AI-enhancement image (the preprocessed original).
    When provided and ``intensity`` < 100, the final render is blended back
    toward it -- this is the "how much AI enhancement" slider: 100 is the
    full enhanced result (the default, matching prior behaviour), 0 is the
    unenhanced original.
    """
    if source is None or source.size == 0:
        raise ValidationError("The enhanced image cannot be edited safely.")
    if mode not in EditMode.values:
        raise ValidationError({"mode": "Choose auto or manual."})

    before = analyze_image_quality(source)
    if mode == EditMode.AUTO:
        controls = _auto_editor_adjustments(before)
        intensity = 100.0
    else:
        requested_adjustments = requested_adjustments or {}
        controls = {
            name: float(requested_adjustments.get(name, 0.0))
            for name in EDITOR_CONTROL_NAMES
        }
        intensity = float(np.clip(requested_adjustments.get("intensity", 100.0), 0.0, 100.0))

    strength = 1.0
    clipping_limit = max(
        settings.ENHANCEMENT_MAX_HIGHLIGHT_CLIPPING_PERCENT,
        before["highlight_clipping_percent"] + 2.0,
    )
    reasons = []
    rendered = source.copy()
    effective = controls
    after = before
    for _attempt in range(10):
        rendered, effective = _render_editor_controls(source, controls, strength)
        after = analyze_image_quality(rendered)
        contrast_floor = before["contrast_stddev"] * 0.70
        if (
            after["highlight_clipping_percent"] <= clipping_limit
            and after["contrast_stddev"] >= contrast_floor
        ):
            break
        if after["highlight_clipping_percent"] > clipping_limit:
            if "highlight_clipping_limited" not in reasons:
                reasons.append("highlight_clipping_limited")
        if after["contrast_stddev"] < contrast_floor:
            if "contrast_loss_limited" not in reasons:
                reasons.append("contrast_loss_limited")
        strength *= 0.78

    intensity_applied = False
    if baseline is not None and intensity < 100.0:
        baseline_reference = baseline
        if baseline_reference.shape[:2] != rendered.shape[:2]:
            baseline_reference = cv2.resize(
                baseline_reference,
                (rendered.shape[1], rendered.shape[0]),
                interpolation=cv2.INTER_CUBIC,
            )
        blend = intensity / 100.0
        rendered = cv2.addWeighted(rendered, blend, baseline_reference, 1.0 - blend, 0)
        after = analyze_image_quality(rendered)
        intensity_applied = True

    return rendered, {
        "mode": mode,
        "policy": "non_destructive_gallery_editor_v1",
        "requested": controls,
        "effective": {name: round(value, 3) for name, value in effective.items()},
        "safety_strength": round(strength, 4),
        "safety_reasons": reasons,
        "quality_before": before,
        "quality_after": after,
        "dimensions_preserved": source.shape[:2] == rendered.shape[:2],
        "intensity": {
            "requested": round(intensity, 2),
            "applied": intensity_applied,
            "policy": "blend_toward_preprocessed_original",
        },
    }


def _read_edited_source(edit):
    source_file = edit.enhancement_result.enhanced_file
    if not source_file:
        raise ValidationError("The enhanced image is unavailable.")
    source_file.open("rb")
    try:
        payload = source_file.read()
    finally:
        source_file.close()
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValidationError("The enhanced image cannot be decoded safely.")
    return decoded


def _read_preprocessed_baseline(preprocessing):
    """Decode the pre-AI-enhancement image, for the intensity blend slider."""
    if not preprocessing.processed_file:
        return None
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    return cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)


def process_image_edit(edit):
    """Render and persist a gallery edit as a separate immutable file version."""
    with transaction.atomic():
        edit = (
            ImageEditVersion.objects.select_for_update()
            .select_related("enhancement_result__preprocessing_result", "owner")
            .get(pk=edit.pk)
        )
        if edit.status == EnhancementStatus.COMPLETED and edit.edited_file:
            return edit
        if edit.status == EnhancementStatus.CANCELLED:
            return edit
        edit.status = EnhancementStatus.PROCESSING
        edit.started_at = edit.started_at or timezone.now()
        edit.failure_reason = ""
        edit.save(
            update_fields=["status", "started_at", "failure_reason", "updated_at"]
        )

    started = time.perf_counter()
    stored_name = None
    try:
        source = _read_edited_source(edit)
        preprocessing = edit.enhancement_result.preprocessing_result
        baseline = _read_preprocessed_baseline(preprocessing)
        rendered, metadata = apply_gallery_adjustments(
            source,
            edit.mode,
            edit.requested_adjustments,
            baseline=baseline,
        )
        payload, extension, output_format = _encode_output(rendered, preprocessing)
        source_stem = Path(edit.enhancement_result.output_name).stem
        output_name = f"{source_stem}_edit_{edit.pk}{extension}"
        edit.edited_file.save(output_name, ContentFile(payload), save=False)
        stored_name = edit.edited_file.name
        edit.status = EnhancementStatus.COMPLETED
        edit.applied_adjustments = metadata["effective"]
        edit.output_name = output_name
        edit.output_format = output_format
        edit.output_width = int(rendered.shape[1])
        edit.output_height = int(rendered.shape[0])
        edit.output_file_size = len(payload)
        edit.processing_duration_ms = max(
            1, round((time.perf_counter() - started) * 1000)
        )
        edit.quality_metadata = metadata
        edit.failure_reason = ""
        edit.finished_at = timezone.now()
        with transaction.atomic():
            ImageEditVersion.objects.filter(
                enhancement_result=edit.enhancement_result,
                is_current=True,
            ).exclude(pk=edit.pk).update(is_current=False)
            edit.is_current = True
            edit.save()
        return edit
    except Exception as error:
        if stored_name:
            edit.edited_file.storage.delete(stored_name)
        edit.status = EnhancementStatus.FAILED
        edit.failure_reason = "The gallery edit could not be rendered safely."
        edit.processing_duration_ms = max(
            1, round((time.perf_counter() - started) * 1000)
        )
        edit.finished_at = timezone.now()
        edit.save(
            update_fields=[
                "status", "failure_reason", "processing_duration_ms",
                "finished_at", "updated_at",
            ]
        )
        if isinstance(error, ValidationError):
            raise
        raise ValidationError("The gallery edit could not be rendered safely.") from error

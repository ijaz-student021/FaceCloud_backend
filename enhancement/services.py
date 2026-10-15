"""OpenCV enhancement services and parameter policies.

Lighting correction is applied to the LAB luminance channel. Colour channels are
left unchanged so the baseline does not intentionally normalize skin tones.
"""

import logging
import math
import time
import threading
import uuid
from datetime import timedelta
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, ValidationError
from PIL import Image, ImageColor, UnidentifiedImageError

from uploads.decoding import to_working_colour
from uploads.models import ImagePreprocessingResult, UploadBatch
from quality_control.models import ImageQualityDecision

from . import policy as rendering_policy
from .controls import (
    CONTROL_NAMES, INTENTS, RENDERER_POLICY_NAME, RENDERER_VERSION, is_neutral,
    recommend_local_adjustments, validate_controls,
)
from .editor_source import (
    attach, read_enhanced_with_alpha, restore_hidden, source_version, transparency_policy, visible_for_measurement,
)
from .tone import render_tone
from .policy import learned_stage_enabled
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
from .denoise_guard import DenoiseReference, judge_candidate
from .face_fidelity import check_face_regions
from .gfpgan import GFPGANUnavailable, restore_faces
from .identity_guard import (
    IDENTITY_VERIFIED,
    IdentityGuardUnavailable,
    apply_identity_guard,
    build_identity_reference,
    identity_status,
)
from .hd_restoration import (
    HDRestorationUnavailable,
    deblur_with_nafnet,
    upscale_with_realesrgan,
)
from .purescale import PureScaleUnavailable, upscale_with_purescale
from .purevision import PureVisionUnavailable, restore_with_purevision
from .zero_dce import ZeroDCEUnavailable, enhance_with_zero_dce


logger = logging.getLogger(__name__)

# Dropped database connections: worth retrying, unlike a bad image.
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)


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

# Smallest smoothed Sobel magnitude treated as a full-strength edge.
_EDGE_REFERENCE_FLOOR = 20.0
# Percentage points of highlight clipping a colour-space round trip can add.
_CLIPPING_ROUNDING_TOLERANCE = 0.1

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
    # The last accepted image before any restoration model (PureVision, EDSR,
    # NAFNet, Real-ESRGAN, PureScale) ran. Guard B judges the final pixels
    # against this, resized only for the comparison, so a model's own changes
    # cannot hide by becoming the reference they are measured against.
    restoration_reference: np.ndarray | None = None
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
    prefilter: dict = field(default_factory=dict)
    quality_guards: dict = field(default_factory=dict)
    post_exposure_noise: dict = field(default_factory=dict)
    adaptive_denoise: dict = field(default_factory=dict)
    resolution_contract: dict = field(default_factory=dict)
    face_fidelity: dict = field(default_factory=dict)
    # What the analysis decided to do and why, before any pixel is changed.
    plan: dict = field(default_factory=dict)
    # One entry per stage: what it produced and whether that was accepted.
    stage_reports: list = field(default_factory=list)
    # The source's embedded faces, prepared at most once and shared by the
    # face-restoration gate and the final identity guard.
    identity_reference: object = None
    identity_reference_reason: str = ""
    identity_reference_state: str = ""
    # Called with a stage name before each expensive stage; raises to stop
    # (cancellation, or another worker having taken the result over).
    checkpoint: object = None
    # Set when face restoration was applied but identity then could not be
    # verified: the tail of the pipeline is run again without it.
    face_restoration_withdrawn: bool = False
    # Reports of a tail pass that was withdrawn and run again (see enhance_bgr).
    discarded_stage_reports: list = field(default_factory=list)
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
        # A stage can run twice (see face_restoration_withdrawn); its time adds up.
        context.stage_timings_ms[stage_name] = round(
            context.stage_timings_ms.get(stage_name, 0.0)
            + (time.perf_counter() - started_at) * 1000,
            3,
        )
        context.stage_order.append(stage_name)


def _auto_level(mean_luminance, highlight_clipping_percent=0.0):
    # Darkness chose the level only to decide how hard to lift. Under the
    # lightweight policy automatic mode never lifts a dark photo (its intent
    # cannot be read from pixels), so darkness no longer raises the level.
    if rendering_policy.active_policy() == rendering_policy.LEARNED:
        if mean_luminance < 65:
            return EnhancementLevel.HIGH
        if mean_luminance < 115:
            return EnhancementLevel.MEDIUM
    # Overexposure needs correcting as much as darkness does; the level only
    # ever followed darkness, so a blown-out photo got the gentlest setting.
    # Judged on clipped highlights, not on brightness: a high-key portrait or a
    # snow scene is bright by intent and must not be darkened.
    if highlight_clipping_percent > 20:
        return EnhancementLevel.HIGH
    if highlight_clipping_percent > 8:
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


# Blur is judged where an image is sharpest, on a copy of fixed size.
#
# The measure is the median, over the strongest 2 % of edges, of the
# Laplacian (curvature) beside the edge divided by the gradient across it:
# roughly the inverse of the edge's width in pixels. A gain cancels out of the
# ratio, so darkening does not read as blur; measuring at a fixed long edge
# makes it a property of the picture rather than of its pixel count; and the
# strongest edges are the in-focus ones, so a soft background (bokeh) or noise
# in flat areas does not decide it. The old measure, the Laplacian variance of
# the full-size image, fell 5-14x with resolution and ~16x with a 4x
# darkening, and read sharp photos as blurred.
#
# Thresholds and the evidence for them: docs/evaluation/2026-10-14-blur-measure.md.
BLUR_PROXY_LONG_EDGE = 1024
BLUR_EDGE_PERCENTILE = 98
# Units: edge_sharpness is a ratio (per pixel at the measuring scale); edge
# strength is grey levels per pixel. Chosen on the calibration half of 498
# photographs and checked on the other half:
# * mild: below the 1st percentile of sharp photographs at every size,
#   exposure, noise and JPEG level tried (about 1 % of sharp photos flagged);
# * severe: the value that best separates a Gaussian blur of sigma 1.2 from one
#   of sigma 2.0 at the measuring scale;
# * uncertain: a tonal range (5th to 95th percentile, grey levels) too small
#   to judge -- a flat or near-black frame. Range, not edge strength: blur
#   weakens edges but keeps the range, so heavy blur is still judged. Below 8
#   levels on the calibration half: 0.6 % of severely blurred variants, 2.7 %
#   of sharp ones (graphics and near-uniform frames). Also uncertain: no edge
#   anywhere -- the strongest gradients under one grey level per pixel, i.e.
#   only quantisation steps (a smooth gradient), which carries no evidence of
#   focus either way (0.4 % of severely blurred variants).
BLUR_SEVERE_BELOW = 0.232
BLUR_MILD_BELOW = 0.422
BLUR_MIN_TONAL_RANGE = 8.0
BLUR_MIN_EDGE_STRENGTH = 1.0


def measure_blur(gray):
    """``{"edge_sharpness", "edge_strength", "verdict", ...}`` for a grayscale image.

    ``verdict`` is ``sharp``, ``mild``, ``severe`` or ``uncertain``: with too
    little tonal range to judge (a flat or almost black frame), nothing is
    claimed, and no blur correction is routed.
    """
    height, width = gray.shape[:2]
    scale = min(1.0, BLUR_PROXY_LONG_EDGE / float(max(height, width)))
    if scale < 1.0:
        gray = cv2.resize(gray, (max(1, round(width * scale)), max(1, round(height * scale))),
                          interpolation=cv2.INTER_AREA)
    working = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 0.5)
    gradient = np.hypot(cv2.Sobel(working, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(working, cv2.CV_32F, 0, 1, ksize=3))
    curvature = cv2.dilate(np.abs(cv2.Laplacian(working, cv2.CV_32F, ksize=3)), np.ones((3, 3), np.uint8))
    threshold = float(np.percentile(gradient, BLUR_EDGE_PERCENTILE))
    edges = gradient >= max(threshold, 1e-6)
    edge_sharpness = float(np.median(curvature[edges] / gradient[edges])) if edges.any() else 0.0
    # Sobel's 3x3 kernel sums to 8x a one-pixel step: divide to get grey levels per pixel.
    edge_strength = threshold / 8.0
    low, high = np.percentile(gray, (5, 95))
    tonal_range = float(high - low)
    if tonal_range < BLUR_MIN_TONAL_RANGE or edge_strength < BLUR_MIN_EDGE_STRENGTH:
        verdict = "uncertain"
    elif edge_sharpness < BLUR_SEVERE_BELOW:
        verdict = "severe"
    elif edge_sharpness < BLUR_MILD_BELOW:
        verdict = "mild"
    else:
        verdict = "sharp"
    return {
        "measure": "edge_sharpness_v1",
        "edge_sharpness": round(edge_sharpness, 4),
        "edge_strength": round(edge_strength, 3),
        "tonal_range": round(tonal_range, 1),
        "measured_at_long_edge": int(round(max(height, width) * scale)),
        "verdict": verdict,
    }


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
    blur = measure_blur(gray)
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
    if blur["verdict"] == "severe":
        defects.append("severe_blur")
    elif blur["verdict"] == "mild":
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
        # Reported for comparison only: it changes with resolution, exposure and
        # noise, so it no longer decides blur (see ``measure_blur``).
        "laplacian_sharpness": round(sharpness, 2),
        "blur": blur,
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


def _verify_edsr_checksum(model_path):
    """The file's SHA-256, which must equal AI_RESTORATION_EDSR_MODEL_SHA256.

    The hash is cached by file identity (path, size, modification time), so a
    loaded model costs no rehash per image, and a replaced file is hashed again.
    """
    from identities.providers import verify_model_checksum

    expected = str(settings.AI_RESTORATION_EDSR_MODEL_SHA256).strip().lower()
    if len(expected) != 64:
        raise RuntimeError("The EDSR checksum is not configured.")
    try:
        return verify_model_checksum(model_path, expected)
    except ValidationError as error:
        raise RuntimeError("The EDSR model failed integrity verification.") from error


def _load_edsr_model(model_path, scale):
    if not hasattr(cv2, "dnn_superres"):
        raise RuntimeError("The OpenCV super-resolution module is not installed.")
    model_path = Path(model_path)
    if not model_path.is_file():
        raise RuntimeError("The configured EDSR model file is unavailable.")
    key = (str(model_path), scale, _verify_edsr_checksum(model_path))
    if key in _SUPERRES_CACHE:
        return _SUPERRES_CACHE[key]
    superres = cv2.dnn_superres.DnnSuperResImpl_create()
    superres.readModel(str(model_path))
    superres.setModel("edsr", scale)
    _SUPERRES_CACHE[key] = superres
    return superres


def restore_low_resolution(image, quality):
    """Apply bounded pretrained EDSR only when the source is low resolution."""
    metadata = {
        "requested": "low_resolution" in quality["defects"],
        "enabled": learned_stage_enabled("AI_RESTORATION_ENABLED"),
        "applied": False,
        "provider": "opencv_dnn_edsr",
        "device": "cpu",
        "scale": settings.AI_RESTORATION_SCALE,
        "fallback_reason": "",
    }
    if not metadata["requested"]:
        return image, metadata
    if not learned_stage_enabled("AI_RESTORATION_ENABLED"):
        metadata["fallback_reason"] = rendering_policy.disabled_reason("AI restoration")
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
        # The absolute limit stops enhancement from *creating* blown highlights.
        # A source that is already over it must not veto the correction that
        # darkens it: only a candidate that adds clipping is held to the limit
        # (a tenth of a point allows for colour-space rounding).
        absolute_clipping_ok = (
            output_quality["highlight_clipping_percent"] <= clipping_limit
            or clipping_growth <= _CLIPPING_ROUNDING_TOLERANCE
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
    if (
        output_quality["highlight_clipping_percent"] > clipping_limit
        and clipping_growth > _CLIPPING_ROUNDING_TOLERANCE
    ):
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


# JPEG codes 8x8 blocks, so heavy compression leaves a step every eighth pixel.
# The ratio below is the strongest of the eight possible grid phases against the
# typical one: about 1.0-1.2 on an uncompressed photo, 1.4 and up from roughly
# quality 50 downward. Both axes must show it, so a striped or brick-like
# subject is not mistaken for compression. Thresholds were set on four
# photographs and then checked, unchanged, on eighteen others.
_BLOCKINESS_STRONG_AXIS = 1.35
_BLOCKINESS_WEAK_AXIS = 1.15
_LIGHTING_DEFECTS = frozenset({"severe_low_light", "low_light", "overexposed_highlights", "low_contrast"})
_BLUR_DEFECTS = frozenset({"severe_blur", "mild_blur"})


def measure_compression_artifacts(image):
    """Detect JPEG blocking: ``{"blockiness": [strong, weak], "detected": bool}``.

    Sharpening and local-contrast enhancement both amplify block edges, so the
    pipeline needs to know before it applies either. Every grid phase is tried,
    so a crop or a rotation of a compressed photo is still recognised. A photo
    resized after compression no longer has an 8-pixel grid and reads as clean,
    which is the safe direction: it is simply processed as before.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    ratios = []
    for axis in (0, 1):
        steps = np.abs(np.diff(gray, axis=axis)).mean(axis=1 - axis)
        usable = (len(steps) // 8) * 8
        if usable < 64:
            return {"blockiness": [1.0, 1.0], "detected": False}
        per_phase = steps[:usable].reshape(-1, 8).mean(axis=0)
        ratios.append(float(per_phase.max() / max(float(np.median(per_phase)), 1e-3)))
    strong, weak = max(ratios), min(ratios)
    return {
        "blockiness": [round(strong, 3), round(weak, 3)],
        "detected": strong >= _BLOCKINESS_STRONG_AXIS and weak >= _BLOCKINESS_WEAK_AXIS,
    }


def _apply_conservative_routing(context):
    """Switch off corrections that have no measured defect to correct.

    Returns the list of decisions made, for the plan. Two rules:

    * Compression artifacts: no sharpening and no local-contrast step, at any
      level or style. Both make block edges more visible, and nothing on this
      path removes them.
    * On the automatic level with the Natural style, a correction needs a
      defect: no lighting defect means no exposure change, no blur defect means
      no sharpening. An explicit level or style is the user asking for the
      correction and is honoured as before.
    """
    defects = set(context.input_quality["defects"])
    policy = context.policy
    decisions = []
    compression = measure_compression_artifacts(context.source_image)
    context.input_quality["compression_artifacts"] = compression
    if compression["detected"]:
        if policy.get("sharpen"):
            policy["sharpen"] = 0
            decisions.append({"stage": "sharpen", "decision": "skipped",
                              "reason": "Compression artifacts: sharpening would emphasise block edges."})
        if policy.get("use_clahe"):
            policy["use_clahe"] = False
            decisions.append({"stage": "exposure_enhancement", "decision": "local_contrast_off",
                              "reason": "Compression artifacts: local contrast would emphasise block edges."})
    automatic = (
        context.requested_level == EnhancementLevel.AUTO and context.style == EnhancementStyle.NATURAL
    )
    # A dark photo may be underexposed or dark by design (low-key portraits,
    # night scenes); pixel statistics cannot tell them apart (the mean luminance
    # and highlight level of real photos form a continuum, not two groups). So
    # in the lightweight policy, automatic mode withholds the lift and says so;
    # an explicit level or style, or the editor's "brighten" intent, asks for it.
    dark = defects & {"low_light", "severe_low_light"}
    withheld = (
        automatic and bool(dark) and "overexposed_highlights" not in defects
        and rendering_policy.active_policy() != rendering_policy.LEARNED
    )
    if withheld:
        policy["skip_exposure"] = True
        policy["exposure_lift_withheld"] = True
        decisions.append({
            "stage": "exposure_enhancement", "decision": "lift_withheld",
            "reason": "Dark photo: whether it is meant to be dark cannot be told from the pixels, so it was "
                      "not brightened. Choose a level (low, medium or high) to lift it.",
        })
    elif automatic and not (defects & _LIGHTING_DEFECTS):
        policy["skip_exposure"] = True
        decisions.append({"stage": "exposure_enhancement", "decision": "skipped",
                          "reason": "No lighting defect was measured."})
    if automatic and not (defects & _BLUR_DEFECTS) and policy.get("sharpen"):
        policy["sharpen"] = 0
        decisions.append({"stage": "sharpen", "decision": "skipped",
                          "reason": "No blur was measured."})
    return decisions


def _planned_upscaler(defects, width, height):
    """The single upscaler this image is routed to, or ``None``.

    Order of preference matches the stages: Real-ESRGAN when the HD pipeline is
    on, otherwise EDSR for a genuinely low-resolution source, otherwise PureScale.
    Only one ever changes the pixel grid; a later one runs only if the earlier
    one was unavailable.
    """
    if learned_stage_enabled("HD_RESTORATION_ENABLED"):
        return "realesrgan_x4plus", "The HD pipeline is enabled."
    if (
        "low_resolution" in defects
        and learned_stage_enabled("AI_RESTORATION_ENABLED")
        and min(width, height) <= settings.AI_RESTORATION_MAX_INPUT_EDGE
    ):
        return "opencv_dnn_edsr", "The source is low resolution."
    if learned_stage_enabled("PURESCALE_ENABLED"):
        return "purescale_safmn_2x", "PureScale is enabled."
    return None, "No upscaler is enabled for this image."


def _build_processing_plan(context):
    """Decide, from the measured defects, which stages have work to do.

    The plan is a record of intent written before any pixel changes. Stages still
    make their own final decision (a model may be unavailable, a candidate may be
    rejected), and ``stage_reports`` records what actually happened.
    """
    quality = context.input_quality
    defects = list(quality["defects"])
    lighting_defects = [
        name for name in defects
        if name in {"severe_low_light", "low_light", "overexposed_highlights", "low_contrast"}
    ]
    blur_defects = [name for name in defects if name in {"severe_blur", "mild_blur"}]
    explicit_style = context.style != EnhancementStyle.NATURAL
    # An image with nothing wrong with its light, noise or contrast gets the
    # gentlest correction the level allows and nothing else by default.
    minimal = (
        not lighting_defects
        and "visible_noise" not in defects
        and context.requested_level == EnhancementLevel.AUTO
        and not explicit_style
    )
    upscaler, upscaler_reason = _planned_upscaler(defects, quality["width"], quality["height"])
    face_restoration_wanted = bool(
        set(defects) & ({"severe_blur", "low_resolution"} | ({"mild_blur"} if settings.GFPGAN_ALLOW_MILD_BLUR else set()))
    )

    def step(planned, reason):
        return {"planned": bool(planned), "reason": reason}

    return {
        "version": "defect_driven_plan_v1",
        "defects": defects,
        "minimal_processing": minimal,
        "applied_level": context.applied_level,
        "exposure_mode": context.policy.get("exposure_mode"),
        "steps": {
            "exposure": step(
                not context.policy.get("skip_exposure"),
                "Dark photo: lift withheld, lighting intent is unknown." if context.policy.get("exposure_lift_withheld")
                else "Low light: learned curves when available, otherwise the OpenCV correction."
                if context.zero_dce["requested"]
                else f"Lighting defects: {', '.join(lighting_defects)}." if lighting_defects
                else "No lighting defect was measured." if context.policy.get("skip_exposure")
                else "No lighting defect; the requested level's balanced correction.",
            ),
            "denoise": step(
                context.applied_level in {EnhancementLevel.MEDIUM, EnhancementLevel.HIGH}
                or context.style == EnhancementStyle.AI_MAX,
                "Decided after exposure, from the noise that correction reveals.",
            ),
            "deblur": step(
                learned_stage_enabled("HD_RESTORATION_ENABLED") and learned_stage_enabled("NAFNET_DEBLUR_ENABLED") and blur_defects,
                "Deblurring is disabled." if not (learned_stage_enabled("HD_RESTORATION_ENABLED") and learned_stage_enabled("NAFNET_DEBLUR_ENABLED"))
                else f"Blur defects: {', '.join(blur_defects)}." if blur_defects
                else "No blur was measured.",
            ),
            "upscale": {**step(upscaler is not None, upscaler_reason), "upscaler": upscaler},
            "face_restoration": step(
                face_restoration_wanted and learned_stage_enabled("GFPGAN_ENABLED"),
                "Blur or low resolution on a face-restoration-enabled deployment."
                if face_restoration_wanted and learned_stage_enabled("GFPGAN_ENABLED")
                else "Face restoration is disabled." if face_restoration_wanted
                else "No blur or low-resolution defect.",
            ),
            "sharpen": step(context.policy.get("sharpen", 0) > 0, f"Blur defects: {', '.join(blur_defects) or 'none'}."),
            "colorization": step(
                context.colorization["requested"] and learned_stage_enabled("COLORIZATION_ENABLED"),
                "The source is near-grayscale." if context.colorization["requested"] else "The source has colour.",
            ),
            "style": step(explicit_style, f"Requested style: {context.style}."),
        },
    }


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
        _auto_level(mean_luminance, context.input_quality["highlight_clipping_percent"])
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
    routing = _apply_conservative_routing(context)
    context.plan = _build_processing_plan(context)
    context.plan["routing"] = routing
    context.plan["compression_artifacts"] = context.input_quality["compression_artifacts"]["detected"]
    return context


_PREFILTER_NLM_MAX_PIXELS = 4_000_000


def _chroma_prefilter(image):
    """Smooth colour noise only: chroma planes denoised on a bounded copy, then re-aligned to the luminance edges."""
    height, width = image.shape[:2]
    ycc = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
    luma, red, blue = cv2.split(ycc)
    scale = min(1.0, 1024.0 / max(height, width))
    small = (max(1, round(width * scale)), max(1, round(height * scale)))
    planes = []
    for plane in (red, blue):
        reduced = cv2.resize(plane, small, interpolation=cv2.INTER_AREA) if scale < 1.0 else plane
        smooth = cv2.fastNlMeansDenoising(reduced, None, 9, 5, 15)
        smooth = cv2.resize(smooth, (width, height), interpolation=cv2.INTER_CUBIC) if scale < 1.0 else smooth
        # a guided filter keeps the smoothed colour from bleeding across luminance edges
        planes.append(cv2.ximgproc.guidedFilter(luma, smooth, 8, 25.0))
    return cv2.cvtColor(cv2.merge((luma, planes[0], planes[1])), cv2.COLOR_YCrCb2BGR)


def _prefilter_for_lift(context, source):
    """The image to lift, and what was done to it. Only a requested lift of a clearly dark photo is filtered."""
    mode = settings.ENHANCEMENT_PREFILTER_MODE
    metadata = {"mode": mode, "applied": False, "reason": ""}
    context.prefilter = metadata
    if mode == "off":
        metadata["reason"] = "switched off"
        return source
    if context.input_quality["mean_luminance"] > settings.ENHANCEMENT_PREFILTER_MAX_LUMINANCE:
        metadata["reason"] = "the photo is not dark enough for a lift to amplify noise"
        return source
    height, width = source.shape[:2]
    if mode == "nlm" and height * width <= _PREFILTER_NLM_MAX_PIXELS:
        filtered = cv2.fastNlMeansDenoisingColored(source, None, 2, 12, 5, 15)
        metadata["method"] = "colour non-local-means (h=2, hColor=12)"
    else:
        filtered = _chroma_prefilter(source)
        metadata["method"] = "chroma-only, guided by luminance"
        if mode == "nlm":
            metadata["reason"] = "too large for non-local-means; chroma-only used"
    metadata["applied"] = True
    metadata["mean_change"] = round(float(np.mean(np.abs(filtered.astype(np.int16) - source.astype(np.int16)))), 3)
    return filtered


def exposure_enhancement_stage(context):
    """Apply learned exposure curves or the auditable OpenCV fallback."""
    source = context.source_image
    working = source
    # A lift withheld by the routing (automatic mode on a dark photo) is not made by Zero-DCE either.
    if (
        context.zero_dce["requested"] and learned_stage_enabled("ZERO_DCE_ENABLED") and context.use_zero_dce
        and not context.policy.get("skip_exposure")
    ):
        try:
            working, zero_metadata = enhance_with_zero_dce(source)
            context.zero_dce.update(zero_metadata)
            context.zero_dce["applied"] = True
        except ZeroDCEUnavailable as error:
            context.zero_dce["fallback_reason"] = str(error)
    elif context.zero_dce["requested"]:
        context.zero_dce["fallback_reason"] = (
            rendering_policy.disabled_reason("Zero-DCE") if not learned_stage_enabled("ZERO_DCE_ENABLED")
            else "The lift was withheld (automatic mode); choose a level to lift."
            if context.policy.get("skip_exposure") else "Zero-DCE was switched off for this run."
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
    if policy.get("skip_exposure"):
        # Nothing about this photo's light was found wanting; leave it alone.
        return context
    source = _prefilter_for_lift(context, source)
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
    # "Edge" is relative to the photo's own 75th-percentile gradient, but never
    # below an absolute floor: in a photo with no real edges (sky, a studio
    # backdrop) the strongest quarter of leftover grain would otherwise count as
    # edges and be protected from denoising, leaving blotches. Measured p75 on
    # real photographs was 34-87, so the floor does not affect them.
    edge_reference = max(float(np.percentile(gradient, 75)), _EDGE_REFERENCE_FLOOR) + 1e-6
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


def _denoise_face_boxes(image):
    """Face boxes for the denoise check, or none when the detector is unavailable."""
    from .face_fidelity import _detect_boxes

    try:
        return _detect_boxes(image)
    except (ValidationError, cv2.error):
        return []


def adaptive_denoise_stage(context):
    """Denoise only where noise was measured, and keep the result only if it paid off.

    Eligibility comes from the image, not from the exposure level: a photo that
    was brightened is not thereby noisy, and a well-exposed one is not thereby
    clean. The denoised candidate is then compared with the image as it was
    immediately before (``DenoiseReference``) and kept, weakened or dropped on
    what it removed and what it cost. Nothing later (sharpening in particular)
    is consulted or adjusted to offset a loss here.
    """
    enabled = bool(settings.ADAPTIVE_DENOISE_ENABLED) or context.style == EnhancementStyle.AI_MAX
    noise_score = context.post_exposure_noise["estimated_noise"]
    before = context.current_image
    minimum_noise = float(settings.DENOISE_MIN_NOISE_REMOVED)
    if enabled:
        requested = _adaptive_denoise_strength(noise_score)
        basis = "measured_noise"
    else:
        # The rollout flag is off: the legacy input-defect policy proposes the
        # strength, and its candidate is judged like any other.
        requested = int(context.policy.get("denoise", 0))
        basis = "feature_flag_disabled_legacy_policy"

    eligibility = {
        "eligible": False,
        "reason": "",
        "basis": basis,
        "noise_score": noise_score,
        "ladder_strength": requested,
        "flat_region_noise": None,
        "minimum_flat_region_noise": minimum_noise,
        # Recorded to show it was not consulted.
        "exposure_level": context.applied_level,
        "depends_on_exposure_level": False,
    }
    reference = None
    if "severe_blur" in context.input_quality.get("defects", ()):
        # The one blur verdict every stage uses (sharpening, deblurring, face
        # restoration, the plan). It does not depend on exposure, so a night
        # photo is not read as blurred; it used to be, which switched denoising
        # off for it until this was judged on the brightened image instead.
        eligibility["reason"] = "The image is severely blurred; denoising would remove what detail is left."
        reason = "severe_blur_protection"
    elif requested <= 0:
        eligibility["reason"] = "The measured noise score is below the lowest denoise step."
        reason = "noise_score_below_ladder"
    else:
        reference = DenoiseReference(before)
        eligibility["flat_region_noise"] = reference.noise_level
        if reference.noise_level < minimum_noise:
            # It cannot remove more noise than is there, so it could not pass.
            eligibility["reason"] = "Too little noise was measured in the flat regions to be worth removing."
            reason = "no_measurable_noise"
            reference = None
        else:
            eligibility["eligible"] = True
            eligibility["reason"] = "Noise was measured in the flat regions."
            reason = "measured_noise"

    metadata = {
        "enabled": enabled,
        "applied": False,
        "strength": 0,
        "requested_strength": requested,
        "reason": reason,
        "method": "edge_aware_nlm_blend",
        "input_noise_score": noise_score,
        "measurement_point": "after_guard_a",
        "reference": "image_immediately_before_denoise",
        "breakpoints": list(settings.ADAPTIVE_DENOISE_BREAKPOINTS),
        "strengths": list(settings.ADAPTIVE_DENOISE_STRENGTHS),
        "eligibility": eligibility,
        "requested": eligibility["eligible"],
    }
    if reference is not None:
        metadata["faces_checked"] = reference.set_faces(_denoise_face_boxes(before))
        candidate = _edge_aware_denoise(before, requested)
        accepted_image, verdict = judge_candidate(reference, candidate)
        decisive = verdict["attempts"][-1 if verdict["accepted"] else 0]
        frame = decisive["frame"]
        metadata["benefit"] = {
            key: frame[key] for key in (
                "noise_before", "noise_after", "noise_removed", "noise_reduction", "removed_residual_whiteness",
            )
        }
        metadata["detail"] = {
            **{key: frame[key] for key in ("detail_retention", "edge_retention")},
            "faces": decisive["faces"],
        }
        metadata["candidate_guard"] = verdict
        metadata["acceptance"] = {key: verdict[key] for key in ("status", "reason", "applied_blend")}
        if verdict["accepted"]:
            context.current_image = accepted_image
            metadata["applied"] = True
            metadata["strength"] = requested
            metadata["reason"] = (
                "measured_noise_accepted" if verdict["status"] == "accepted"
                else "measured_noise_accepted_at_reduced_strength"
            )
        else:
            context.current_image = before
            metadata["reason"] = "candidate_rejected"
            metadata["fallback_reason"] = verdict["reason"]
    else:
        metadata["acceptance"] = {"status": "not_attempted", "reason": eligibility["reason"], "applied_blend": 0.0}
    context.adaptive_denoise = metadata
    context.restoration_reference = context.current_image
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
        "enabled": bool(learned_stage_enabled("PUREVISION_ENABLED")),
        "requested": bool(learned_stage_enabled("PUREVISION_ENABLED")),
        "applied": False,
        "fallback_reason": "",
    }
    if not learned_stage_enabled("PUREVISION_ENABLED"):
        metadata["fallback_reason"] = rendering_policy.disabled_reason("PureVision")
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
    if learned_stage_enabled("HD_RESTORATION_ENABLED"):
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
    return context


def hd_restoration_stage(context):
    """Optionally deblur with NAFNet and create a 2x/4x HD display output."""
    metadata = {
        "enabled": bool(learned_stage_enabled("HD_RESTORATION_ENABLED")),
        "requested": bool(learned_stage_enabled("HD_RESTORATION_ENABLED")),
        "applied": False,
        "deblur": {
            "enabled": bool(learned_stage_enabled("NAFNET_DEBLUR_ENABLED")),
            "requested": bool(learned_stage_enabled("HD_RESTORATION_ENABLED") and learned_stage_enabled("NAFNET_DEBLUR_ENABLED")),
            "applied": False,
        },
        "super_resolution": {"requested": bool(learned_stage_enabled("HD_RESTORATION_ENABLED")), "applied": False},
        "fallback_reason": "",
        "recognition_policy": "display_output_only; recognition_uses_original",
    }
    if not learned_stage_enabled("HD_RESTORATION_ENABLED"):
        metadata["fallback_reason"] = rendering_policy.disabled_reason("HD restoration")
        context.hd_restoration = metadata
        return context
    source_dimensions = [int(context.current_image.shape[1]), int(context.current_image.shape[0])]
    working = context.current_image
    # Without an analysis (a stage called on its own) there is nothing to gate on.
    defects = context.input_quality.get("defects")
    blur_measured = defects is None or bool({"mild_blur", "severe_blur"} & set(defects))
    if learned_stage_enabled("NAFNET_DEBLUR_ENABLED") and not blur_measured:
        # A deblurring network run on a sharp photo can only invent or remove
        # detail; it is not run without measured blur.
        metadata["deblur"]["fallback_reason"] = "No blur was measured; deblurring was not needed."
    elif learned_stage_enabled("NAFNET_DEBLUR_ENABLED"):
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
    except HDRestorationUnavailable as error:
        metadata["fallback_reason"] = str(error)
        context.warnings.append(str(error))
        # The deblurred image already passed its own fidelity guard and sits on
        # the source pixel grid, so it stays the result when only the upscale
        # failed. ``applied`` remains False: no HD output grid was produced.
        if metadata["deblur"]["applied"]:
            context.current_image = working
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
        "enabled": bool(learned_stage_enabled("PURESCALE_ENABLED")),
        "requested": bool(
            learned_stage_enabled("PURESCALE_ENABLED")
            and not context.hd_restoration.get("applied")
            and not context.restoration.get("applied")
        ),
        "applied": False,
        "fallback_reason": "",
    }
    if not learned_stage_enabled("PURESCALE_ENABLED"):
        metadata["fallback_reason"] = rendering_policy.disabled_reason("PureScale")
        context.purescale = metadata
        return context
    if context.hd_restoration.get("applied"):
        metadata["fallback_reason"] = "HD restoration already produced a super-resolution candidate."
        context.purescale = metadata
        return context
    if context.restoration.get("applied"):
        metadata["fallback_reason"] = "EDSR already upscaled this image; a second upscaler would stack scale factors."
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
        "enabled": bool(learned_stage_enabled("GFPGAN_ENABLED")),
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
    if not learned_stage_enabled("GFPGAN_ENABLED"):
        metadata["fallback_reason"] = rendering_policy.disabled_reason("Private face restoration")
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


def _identity_model_configured():
    """Whether an identity model is configured at all (no model is loaded)."""
    from identities.providers import DisabledIdentityProvider, get_identity_provider

    return not isinstance(get_identity_provider(), DisabledIdentityProvider)


NO_IDENTITY_MODEL_REASON = "No identity model is configured, so identity was not checked."


def _prepare_identity_reference(context):
    """Embed the source's faces once: ``(state, reason)``.

    ``state`` is ``ready`` (a comparison is possible), ``inconclusive`` (the
    source has no face reliable enough to compare) or ``unavailable`` (no
    consent, the guard is off, or the detector/identity model is missing).
    """
    if context.identity_reference_state:
        return context.identity_reference_state, context.identity_reference_reason
    state, reason = "unavailable", ""
    if not settings.IDENTITY_GUARD_ENABLED:
        reason = "The identity guard is disabled."
    elif not context.allow_biometric_processing:
        reason = "Biometric consent has not been granted."
    else:
        try:
            reference, reason = build_identity_reference(context.source_image)
            context.identity_reference = reference
            state = "ready" if reference is not None else "inconclusive"
            if reference is None and not _identity_model_configured():
                # Nothing could have been compared even if a face had been found.
                state, reason = "unavailable", NO_IDENTITY_MODEL_REASON
        except IdentityGuardUnavailable as error:
            reason = str(error)
        except (cv2.error, ValueError, TypeError):
            logger.exception("The identity reference could not be prepared.")
            reason = "The identity guard failed safely."
    context.identity_reference_state = state
    context.identity_reference_reason = reason
    return state, reason


BYPASS_AI_MAX = "ai_max_style"
BYPASS_OPERATOR = "operator_setting"


def _identity_requirement_bypass(context):
    """Why face restoration may run without a verified identity, or ``""``.

    Named so it can be reported: a result produced under a bypass must say so
    rather than leave a reader to assume the usual check applied.
    """
    if not settings.GFPGAN_REQUIRE_IDENTITY_CHECK:
        return BYPASS_OPERATOR
    if context.style == EnhancementStyle.AI_MAX:
        return BYPASS_AI_MAX
    return ""


def identity_verification_summary(context):
    """One explicit statement of what was and was not verified about identity.

    ``verified`` is true only when the saved image's faces were compared with
    the source's and held. A check that was skipped, unavailable or bypassed is
    never reported as verified.
    """
    status = identity_status(context.identity_guard)
    verified = status == IDENTITY_VERIFIED
    restored = bool(context.face_restoration.get("applied"))
    bypass = _identity_requirement_bypass(context)
    unverified_restoration = restored and not verified
    notice = ""
    if unverified_restoration:
        cause = {
            BYPASS_AI_MAX: "AI Max does not require an identity check",
            BYPASS_OPERATOR: "this deployment does not require an identity check",
        }.get(bypass, "the identity check did not confirm it")
        notice = (
            "Faces in this image were generatively restored without a verified "
            f"identity check ({cause}; identity: {status}). A restored face may "
            "not match the person in the original."
        )
    elif bypass == BYPASS_AI_MAX and not verified:
        notice = (
            f"AI Max does not require an identity check, and identity was not verified ({status})."
        )
    return {
        "status": status,
        "verified": verified,
        "reason": context.identity_guard.get("fallback_reason", ""),
        "required_for_face_restoration": not bypass,
        "requirement_bypass": bypass or None,
        "face_restoration_applied": restored,
        "face_restoration_unverified": unverified_restoration,
        "notice": notice,
    }


def _face_restoration_gate(context):
    """Refuse generative face restoration when identity cannot be checked.

    GFPGAN synthesises facial detail. Without a source face to compare the
    result with, nothing can tell a restored face from a changed one, so the
    default modes do not run it. The explicit AI Max style is the user asking
    for the strongest result and is not gated.
    """
    if _identity_requirement_bypass(context):
        return ""
    state, reason = _prepare_identity_reference(context)
    if state == "ready":
        return ""
    return (
        "Face restoration was not applied because identity cannot be verified "
        f"for this image ({reason or state})."
    )


def face_restoration_stage(context):
    """Restore only eligible degraded faces; keep the background unchanged."""
    if context.face_restoration_withdrawn:
        return context  # the withdrawn record written by enhance_bgr stands
    refusal = ""
    if learned_stage_enabled("GFPGAN_ENABLED") and context.plan.get("steps", {}).get("face_restoration", {}).get("planned", True):
        refusal = _face_restoration_gate(context)
    if refusal:
        restored, metadata = context.current_image, {
            "requested": True,
            "enabled": True,
            "applied": False,
            "provider": "private_gfpgan_v1_4",
            "fallback_reason": refusal,
            "identity_gate": "refused",
            "identity_embedding_verified": False,
        }
    else:
        restored, metadata = restore_degraded_faces(
            context.current_image,
            context.input_quality,
        )
    if not refusal and metadata.get("enabled"):
        metadata["identity_gate"] = {
            BYPASS_AI_MAX: "bypassed_ai_max", BYPASS_OPERATOR: "not_enforced",
        }.get(_identity_requirement_bypass(context), "passed")
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


def _guard_b_reference(context):
    """The pre-restoration image on the output grid, for comparison only.

    When an upscaler changed the pixel grid the reference is a plain bicubic
    resize of the last image accepted before restoration. It adds no detail of
    its own, so anything a model changed in brightness, contrast, clipping or
    colour shows up as a difference instead of being part of the baseline.
    """
    reference = context.restoration_reference
    basis = "pre_restoration_output"
    if reference is None:
        reference = context.source_image
        basis = "original_source"
    output_height, output_width = context.current_image.shape[:2]
    resized = reference.shape[:2] != (output_height, output_width)
    if resized:
        reference = cv2.resize(
            reference, (output_width, output_height), interpolation=cv2.INTER_CUBIC,
        )
    return reference, {
        "basis": basis,
        "resized_for_comparison": resized,
        "dimensions": [int(output_width), int(output_height)],
    }


def output_guard_b_stage(context):
    """Apply the final safety guard after denoise, EDSR, and sharpening."""
    reference, reference_metadata = _guard_b_reference(context)
    reference_quality = analyze_image_quality(reference)
    guarded, output_quality, metadata = _quality_guard(
        reference,
        context.current_image,
        context.applied_level,
        reference_quality,
        learned_model=False,
    )
    metadata["stage"] = "final_output"
    metadata["reference"] = reference_metadata
    context.current_image = guarded
    context.output_quality = output_quality
    context.quality_guards["guard_b"] = metadata
    return context


def face_fidelity_stage(context):
    """Check each detected face on its own against the pre-restoration image."""
    if not settings.FACE_FIDELITY_ENABLED:
        context.face_fidelity = {"enabled": False, "status": "disabled", "faces_checked": 0, "faces_adjusted": 0}
        return context
    reference = context.restoration_reference
    if reference is None:
        reference = context.source_image
    if reference is context.current_image:
        # Nothing ran after the reference was taken, so there is nothing to judge.
        context.face_fidelity = {
            "enabled": True, "status": "not_required", "faces_checked": 0, "faces_adjusted": 0,
            "fallback_reason": "No restoration or sharpening changed the image.",
        }
        return context
    checked, metadata = check_face_regions(reference, context.current_image)
    context.current_image = checked
    context.face_fidelity = metadata
    if metadata["faces_adjusted"]:
        context.warnings.append(
            "Enhancement was reduced on a face that moved too far from the source "
            "in texture, structure, colour or highlights."
        )
    if metadata.get("status") == "unresolved":
        context.warnings.append(
            "A face still measured outside the fidelity limits after it was restored to the source."
        )
    _reconcile_face_restoration(context, metadata)
    return context


def _reconcile_face_restoration(context, face_check):
    """Stop reporting a GFPGAN restoration that the face check took back out.

    The face check judges against the image before any restoration. When it
    put every face it checked back to that image (strength 0) and checked at
    least as many faces as GFPGAN restored, no restored face is left in the
    output, so the result must not claim one (in the algorithm label, the
    identity notice or the stage report). A partial reduction leaves restored
    detail in place and is reported as applied.
    """
    restoration = context.face_restoration
    faces = face_check.get("faces") or []
    if not restoration.get("applied") or not faces:
        return
    if any(face.get("applied_strength", 1.0) > 0.0 for face in faces):
        return
    if len(faces) < int(restoration.get("faces_restored") or 1):
        return
    reason = "Face restoration was undone by the face check: every restored face was put back to the source."
    context.face_restoration = {
        **restoration, "applied": False, "reverted_by_face_check": True, "fallback_reason": reason,
    }
    context.warnings.append(reason)
    for report in context.stage_reports:
        if report["stage"] == "face_restoration" and report.get("status") == "accepted":
            report["status"] = "reverted"
            report["fallback_reason"] = reason


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
    # At most one of these ever changes the pixel grid: EDSR only runs when HD
    # restoration is disabled, and PureScale refuses to run after either of
    # them. PureVision and NAFNet never resize.
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
        "enabled": bool(learned_stage_enabled("COLORIZATION_ENABLED")),
        "requested": context.colorization["requested"],
        "applied": False,
        "fallback_reason": "",
    }
    if not learned_stage_enabled("COLORIZATION_ENABLED"):
        metadata["fallback_reason"] = rendering_policy.disabled_reason("Colorization")
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
    # The face-restoration gate may already have embedded the source's faces
    # (or found there is nothing to compare); reuse that rather than repeat it.
    state = context.identity_reference_state
    if state in {"inconclusive", "unavailable"}:
        metadata["status"] = "skipped"
        metadata["fallback_reason"] = context.identity_reference_reason
        metadata["reference_checked"] = state == "inconclusive"
        context.identity_guard = metadata
        return context
    prepared = {"reference": context.identity_reference} if state == "ready" else {}
    try:
        guarded, guard_metadata = apply_identity_guard(
            context.source_image, context.current_image, **prepared,
        )
        metadata.update(guard_metadata)
        context.current_image = guarded
        if (
            metadata.get("status") == "skipped"
            and metadata.get("reference_checked")
            and not _identity_model_configured()
        ):
            metadata["reference_checked"] = False
            metadata["fallback_reason"] = NO_IDENTITY_MODEL_REASON
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


class PipelineInterrupted(Exception):
    """The pipeline was told to stop; no output must be published."""


class EnhancementCancelled(PipelineInterrupted):
    """The user cancelled the batch while this image was being enhanced."""


class EnhancementSuperseded(PipelineInterrupted):
    """Another worker now owns this result."""


# (name, function, optional, metadata attribute on the context).
# A *required* stage measures or guards: if it cannot run, nothing downstream can
# be trusted, so its error fails the enhancement. An *optional* stage proposes a
# candidate: if it fails, the image it was given is kept and the pipeline goes on.
_HEAD_STAGES = (
    ("analyze_original", analyze_original_stage, False, None),
    ("exposure_enhancement", exposure_enhancement_stage, True, "zero_dce"),
    ("output_guard_a", output_guard_a_stage, False, None),
    ("measure_post_exposure_noise", measure_post_exposure_noise_stage, False, None),
    ("adaptive_denoise", adaptive_denoise_stage, True, "adaptive_denoise"),
    ("purevision_restoration", purevision_restoration_stage, True, "purevision"),
    ("low_resolution_restoration", low_resolution_restoration_stage, True, "restoration"),
    ("hd_restoration", hd_restoration_stage, True, "hd_restoration"),
    ("purescale_super_resolution", purescale_super_resolution_stage, True, "purescale"),
)
_TAIL_STAGES = (
    ("face_restoration", face_restoration_stage, True, "face_restoration"),
    ("sharpen", sharpen_stage, True, None),
    ("output_guard_b", output_guard_b_stage, False, None),
    ("face_fidelity", face_fidelity_stage, True, "face_fidelity"),
    ("colorization", colorization_stage, True, "colorization"),
    ("dynamic_style", dynamic_style_stage, True, "dynamic_style"),
    ("identity_guard", identity_guard_stage, True, "identity_guard"),
)
# Stages that may run a model or a slow filter: cancellation is checked first.
_CHECKPOINT_STAGES = frozenset({
    "exposure_enhancement", "adaptive_denoise", "purevision_restoration",
    "low_resolution_restoration", "hd_restoration", "purescale_super_resolution",
    "face_restoration", "colorization", "identity_guard",
})
_REPORTED_KEYS = (
    "provider", "model", "device", "tile_size", "memory_retries",
    "checkpoint_sha256_prefix", "strength", "output_blend",
    "requested_strength", "acceptance", "benefit", "detail", "tiles", "tiles_rejected",
)


def _failed_stage_metadata(context, attribute, reason):
    """A complete "did not run" record for a stage that raised."""
    previous = getattr(context, attribute) or {}
    metadata = {
        **previous,
        "requested": bool(previous.get("requested", True)),
        "applied": False,
        "failed": True,
        "fallback_reason": reason,
    }
    if attribute == "hd_restoration":
        metadata.update({
            "enabled": bool(learned_stage_enabled("HD_RESTORATION_ENABLED")),
            "deblur": {"applied": False, "fallback_reason": reason},
            "super_resolution": {"applied": False},
        })
    elif attribute == "adaptive_denoise":
        metadata.update({"strength": 0, "reason": "stage_failed"})
    elif attribute == "identity_guard":
        metadata.update({"enabled": bool(settings.IDENTITY_GUARD_ENABLED), "status": "skipped"})
    elif attribute == "face_fidelity":
        metadata.update({"enabled": True, "status": "unavailable", "faces_checked": 0, "faces_adjusted": 0})
    return metadata


def _stage_report(context, name, optional, attribute, failed, changed, duration_ms):
    """What one stage produced, in the same shape for every stage."""
    metadata = (getattr(context, attribute) or {}) if attribute else {}
    report = {"stage": name, "changed_pixels": bool(changed), "duration_ms": duration_ms}
    if failed:
        status = "failed"
    elif not optional:
        status = "completed"
    elif attribute in {"identity_guard", "face_fidelity"}:
        status = "completed"
        report["outcome"] = metadata.get("status", "")
    elif name == "exposure_enhancement":
        status = "accepted" if changed else "skipped"
        if not metadata.get("applied"):
            metadata = {"provider": "opencv_lab_gamma_clahe", "device": "cpu",
                        "fallback_reason": metadata.get("fallback_reason", "")}
    elif attribute is None:
        status = "accepted" if changed else "skipped"
    elif metadata.get("applied"):
        status = "accepted"
    elif attribute == "hd_restoration" and metadata.get("deblur", {}).get("applied"):
        status = "partially_accepted"
    elif metadata.get("identity_gate") == "refused" or metadata.get("candidate_guard", {}).get("accepted") is False:
        status = "rejected"
    elif metadata.get("fallback_reason") and metadata.get("requested") and metadata.get("enabled", True):
        status = "fallback"
    else:
        status = "skipped"
    report["status"] = status
    for key in _REPORTED_KEYS:
        if metadata.get(key) not in (None, ""):
            report[key] = metadata[key]
    if attribute == "hd_restoration":
        report["models"] = [
            {key: part[key] for key in ("provider", "device", "tile_size", "memory_retries") if key in part}
            | {"applied": bool(part.get("applied"))}
            for part in (metadata.get("deblur", {}), metadata.get("super_resolution", {}))
        ]
    if metadata.get("fallback_reason"):
        report["fallback_reason"] = metadata["fallback_reason"]
    return report


def _run_stage(context, name, function, optional, attribute):
    """Run one stage, keeping the last safe image if an optional stage fails."""
    if context.checkpoint is not None and name in _CHECKPOINT_STAGES:
        context.checkpoint(name)
    before = context.current_image
    failed = False
    started = time.perf_counter()
    with _timed_pipeline_stage(context, name):
        try:
            function(context)
        except (PipelineInterrupted, SoftTimeLimitExceeded, *TRANSIENT_INFRASTRUCTURE_ERRORS):
            raise
        except Exception:
            if not optional:
                raise
            failed = True
            logger.exception("Enhancement stage %s failed; the previous image was kept.", name)
            context.current_image = before
            reason = f"The {name.replace('_', ' ')} stage failed safely; the previous image was kept."
            if attribute:
                setattr(context, attribute, _failed_stage_metadata(context, attribute, reason))
            context.warnings.append(reason)
    if attribute and isinstance(getattr(context, attribute), dict):
        stage_metadata = getattr(context, attribute)
        rejected = sum(
            int(part.get("tiles_rejected") or 0)
            for part in (stage_metadata, stage_metadata.get("deblur") or {}, stage_metadata.get("super_resolution") or {})
            if isinstance(part, dict)
        )
        if rejected:
            context.warnings.append(
                f"The {name.replace('_', ' ')} model diverged on {rejected} tile(s); "
                "those areas were left unrestored."
            )
    if name == "adaptive_denoise":
        # Whatever happened, this is the last image before any restoration model.
        context.restoration_reference = context.current_image
    if attribute and isinstance(getattr(context, attribute), dict):
        getattr(context, attribute)["stage_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    context.stage_reports.append(_stage_report(
        context, name, optional, attribute, failed,
        # A guard returns a new array even when it kept every pixel, so identity
        # alone would report a change that did not happen.
        changed=(
            context.current_image is not before
            and (
                context.current_image.shape != before.shape
                or not np.array_equal(context.current_image, before)
            )
        ),
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    ))


def _face_restoration_needs_withdrawing(context):
    """True when faces were generatively restored but identity was not confirmed."""
    return (
        context.face_restoration.get("applied")
        and settings.GFPGAN_REQUIRE_IDENTITY_CHECK
        and context.style != EnhancementStyle.AI_MAX
        and not context.face_restoration_withdrawn
        and identity_status(context.identity_guard) != IDENTITY_VERIFIED
    )


def enhance_bgr(
    image, requested_level=EnhancementLevel.AUTO, style=EnhancementStyle.NATURAL,
    allow_biometric_processing=True, use_zero_dce=True, checkpoint=None,
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

    ``checkpoint`` is called with a stage name before each expensive stage and
    may raise ``PipelineInterrupted`` to stop (cancellation, lost ownership).

    The source array is never modified. Every stage that changes pixels proposes
    a candidate; ``stage_reports`` records whether it was accepted and why not.
    """
    if style == EnhancementStyle.AI_MAX:
        requested_level = EnhancementLevel.HIGH
    context = EnhancementPipelineContext(
        image, requested_level, style=style,
        allow_biometric_processing=allow_biometric_processing,
        use_zero_dce=use_zero_dce, checkpoint=checkpoint,
    )
    for stage in _HEAD_STAGES:
        _run_stage(context, *stage)
    before_tail = context.current_image
    reports_before_tail = len(context.stage_reports)
    warnings_before_tail = len(context.warnings)
    for stage in _TAIL_STAGES:
        _run_stage(context, *stage)
    if _face_restoration_needs_withdrawing(context):
        # The restored face could not be shown to be the same person, so the
        # last image without generative restoration is finished instead.
        status = identity_status(context.identity_guard)
        reason = (
            "Face restoration was withdrawn because identity could not be verified "
            f"on the restored result ({status})."
        )
        withdrawn = {**context.face_restoration, "applied": False, "withdrawn": True, "fallback_reason": reason}
        context.face_restoration_withdrawn = True
        context.face_restoration = withdrawn
        # The first tail's image is discarded, so are its reports and warnings:
        # what is kept describes the image that is saved. The discarded reports
        # stay available for audit.
        context.discarded_stage_reports = context.stage_reports[reports_before_tail:]
        del context.stage_reports[reports_before_tail:]
        del context.warnings[warnings_before_tail:]
        context.warnings.append(reason)
        context.current_image = before_tail
        for stage in _TAIL_STAGES:
            _run_stage(context, *stage)
    _run_stage(context, "validate_output", validate_output_stage, False, None)

    identity_verification = identity_verification_summary(context)
    # True when every kept stage left the pixels alone: the photo had no defect
    # this pipeline corrects. The output is then the source, re-encoded.
    # Discarded passes (above) are not counted.
    no_correction_needed = not any(entry["changed_pixels"] for entry in context.stage_reports)
    context.plan["no_correction_needed"] = no_correction_needed
    # The restoration stage could not know the final verdict; record it now.
    context.face_restoration["identity_embedding_verified"] = bool(
        context.face_restoration.get("applied") and identity_verification["verified"]
    )
    if identity_verification["face_restoration_unverified"]:
        context.warnings.append(identity_verification["notice"])
    normalized_comparison = compare_image_quality(
        context.source_image,
        context.current_image,
    )
    # Colorization, the style preset and the identity guard all run after guard
    # B, so its measurement is not the saved image's. Measure the final pixels
    # (already done above when the grid is unchanged).
    if context.current_image.shape[:2] == context.source_image.shape[:2]:
        context.output_quality = normalized_comparison["after"]
    else:
        context.output_quality = analyze_image_quality(context.current_image)
    if (
        context.zero_dce["requested"] and not context.zero_dce["applied"]
        and learned_stage_enabled("ZERO_DCE_ENABLED")  # only a model that could have run is "unavailable"
    ):
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
            "Severe blur or low resolution cannot be corrected by the lightweight "
            "pipeline; the baseline avoids inventing detail."
            if not rendering_policy.describe()["heavy_restoration_available"] else
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
    if context.hd_restoration["deblur"]["applied"]:
        algorithm = f"{algorithm}+nafnet_gopro"
    if context.hd_restoration["applied"]:
        algorithm = f"{algorithm}+realesrgan_x4plus"
    if context.purescale["applied"]:
        algorithm = f"{algorithm}+purescale_safmn_2x"
    if context.colorization["applied"]:
        algorithm = f"{algorithm}+colorization_siggraph17"
    if context.dynamic_style["applied"]:
        algorithm = f"{algorithm}+dynamic_vivid_boost"
    if context.identity_guard.get("applied"):
        algorithm = f"{algorithm}+identity_guard"

    render_policy = rendering_policy.describe()
    ai_max = None
    if context.style == EnhancementStyle.AI_MAX:
        ai_max = {
            "requested": True,
            "applied_level": context.applied_level,
            "heavy_restoration_available": render_policy["heavy_restoration_available"],
            "behaviour": (
                "AI Max applies the strongest local correction (high level; denoising only where noise is measured). "
                "Face restoration, super-resolution and the other learned stages are available "
                "only under the learned_v1 rendering policy; this result used "
                f"{render_policy['name']}."
                if not render_policy["heavy_restoration_available"] else
                "AI Max also allows the enabled learned stages."
            ),
        }
        if not render_policy["heavy_restoration_available"]:
            context.warnings.append(
                "AI Max used the strongest local correction only: heavy restoration is not available "
                f"under the {render_policy['name']} rendering policy."
            )

    parameters = {
        **context.policy,
        "style": context.style,
        "dynamic_style": context.dynamic_style,
        "identity_guard": context.identity_guard,
        "mean_input_luminance": context.input_quality["mean_luminance"],
        "algorithm": algorithm,
        "pipeline_version": "staged_candidate_pipeline_v4",
        "render_policy": render_policy,
        **({"ai_max": ai_max} if ai_max else {}),
        "colour_policy": (
            "learned_rgb_curves"
            if context.zero_dce["applied"]
            else "preserve_lab_chroma_channels"
        ),
        "quality_before": context.input_quality,
        "quality_after": context.output_quality,
        "quality_after_measurement_point": "final_output",
        "plan": context.plan,
        "no_correction_needed": no_correction_needed,
        "stage_reports": context.stage_reports,
        "discarded_stage_reports": context.discarded_stage_reports,
        "identity_status": identity_verification["status"],
        "identity_verification": identity_verification,
        "face_fidelity": context.face_fidelity,
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
        "prefilter": context.prefilter,
        "stage_order": context.stage_order,
        "stage_timings_ms": context.stage_timings_ms,
        "feature_flags": {
            "adaptive_denoise_enabled": settings.ADAPTIVE_DENOISE_ENABLED,
            "gfpgan_enabled": learned_stage_enabled("GFPGAN_ENABLED"),
            "gfpgan_queue_enabled": settings.GFPGAN_QUEUE_ENABLED,
            "hd_restoration_enabled": learned_stage_enabled("HD_RESTORATION_ENABLED"),
            "nafnet_deblur_enabled": learned_stage_enabled("NAFNET_DEBLUR_ENABLED"),
            "purevision_enabled": learned_stage_enabled("PUREVISION_ENABLED"),
            "purescale_enabled": learned_stage_enabled("PURESCALE_ENABLED"),
            "colorization_enabled": learned_stage_enabled("COLORIZATION_ENABLED"),
            "identity_guard_enabled": settings.IDENTITY_GUARD_ENABLED,
            "face_fidelity_enabled": settings.FACE_FIDELITY_ENABLED,
            "gfpgan_require_identity_check": settings.GFPGAN_REQUIRE_IDENTITY_CHECK,
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


def _visible_mean_colour(pixels, alpha, fallback):
    """Average colour of the pixels that can be seen, or ``fallback`` if none can."""
    visible = alpha > 0
    if not visible.any():
        return fallback
    return np.rint(pixels[visible].mean(axis=0)).astype(np.uint8)


def _read_full_resolution_original(result, keep_alpha=False, decoding=None):
    """Decode the EXIF-oriented original without a resize or destructive overwrite.

    The original is brought into the working format (8-bit sRGB) by
    ``uploads.decoding.to_working_colour``; what that converted (bit depth,
    colour profile) is added to the ``decoding`` dict when one is passed.

    With ``keep_alpha`` the return value is ``(bgr, alpha)``: ``alpha`` is the
    source's transparency (``None`` when it has none), and the colour channels
    are the source's own, not a blend with the background, so re-attaching the
    alpha later leaves no pale fringe on soft edges. Fully transparent pixels,
    whose stored colour is arbitrary, take the average colour of the visible
    ones, so an invisible area cannot pass for blown highlights or deep shadow
    in the exposure analysis.
    """
    original = result.image.original_file
    if not original:
        raise ValidationError("The original image is unavailable.")
    original.open("rb")
    alpha = None
    try:
        with Image.open(original) as source:
            source.load()
            working, notes = to_working_colour(source)
            if decoding is not None:
                decoding.update(notes)
            if working.mode == "RGBA":
                rgba = working
                background_colour = ImageColor.getrgb(settings.PREPROCESS_TRANSPARENT_BACKGROUND)
                straight = np.asarray(rgba, dtype=np.uint8)
                if keep_alpha and int(straight[:, :, 3].min()) < 255:
                    alpha = straight[:, :, 3].copy()
                    rgb = straight[:, :, :3].copy()
                    rgb[alpha == 0] = _visible_mean_colour(rgb, alpha, background_colour[:3])
                else:
                    background = Image.new("RGB", rgba.size, background_colour)
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    rgb = np.asarray(background, dtype=np.uint8)
            else:
                rgb = np.asarray(working.convert("RGB"), dtype=np.uint8)
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValidationError("The original image cannot be decoded safely.") from error
    finally:
        original.close()
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return (bgr, alpha) if keep_alpha else bgr


def _read_standardized_with_alpha(preprocessing):
    """The standardized copy as ``(bgr, alpha)``; ``alpha`` is ``None`` when opaque."""
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("The image must finish preprocessing before enhancement.")
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.dtype != np.uint8 or decoded.ndim != 3 or decoded.shape[2] != 4:
        return _read_standardized_image(preprocessing), None
    alpha = decoded[:, :, 3].copy()
    if int(alpha.min()) == 255:
        return np.ascontiguousarray(decoded[:, :, :3]), None
    bgr = np.ascontiguousarray(decoded[:, :, :3])
    background = ImageColor.getrgb(settings.PREPROCESS_TRANSPARENT_BACKGROUND)[:3]
    bgr[alpha == 0] = _visible_mean_colour(bgr, alpha, background[::-1])
    return bgr, alpha


def _read_enhancement_source(result, decoding=None):
    if settings.ENHANCEMENT_PRESERVE_ORIGINAL_RESOLUTION:
        try:
            return _read_full_resolution_original(result, decoding=decoding), "full_resolution_original"
        except ValidationError:
            # Legacy rows can legitimately have only their standardized file.
            pass
    return _read_standardized_image(result.preprocessing_result), "standardized_fallback"


def _read_enhancement_source_with_alpha(result, decoding=None):
    """``(bgr, alpha, strategy)`` for a result whose output format keeps transparency."""
    if settings.ENHANCEMENT_PRESERVE_ORIGINAL_RESOLUTION:
        try:
            bgr, alpha = _read_full_resolution_original(result, keep_alpha=True, decoding=decoding)
            return bgr, alpha, "full_resolution_original"
        except ValidationError:
            pass
    bgr, alpha = _read_standardized_with_alpha(result.preprocessing_result)
    return bgr, alpha, "standardized_fallback"


def _attach_alpha(image, alpha):
    """Put the source's transparency back on the enhanced image, at its size."""
    height, width = image.shape[:2]
    if alpha.shape[:2] != (height, width):
        shrinking = alpha.shape[0] > height
        alpha = cv2.resize(
            alpha, (width, height),
            interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR,
        )
    return np.dstack((image, alpha))


def _verify_encoded_output(payload, image, output_format):
    """Decode what will be stored and confirm it is the image that was produced.

    A file that cannot be decoded, has the wrong size or channels, or (for a
    lossy format) differs from the enhanced pixels by more than the configured
    error is never saved.
    """
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.dtype != np.uint8:
        raise ValidationError("The encoded output could not be read back.")
    if decoded.ndim == 2:
        decoded = decoded[:, :, None]
    if decoded.shape != image.shape:
        raise ValidationError("The encoded output does not match the enhanced image.")
    difference = cv2.absdiff(decoded, image)
    mean_error = float(difference.mean())
    limit = 0.0 if output_format == "png" else float(settings.ENHANCEMENT_MAX_ENCODING_ERROR)
    if mean_error > limit:
        raise ValidationError("The encoded output lost more detail than the configured limit allows.")
    mean_squared = float(np.mean(difference.astype(np.float32) ** 2))
    return {
        "verified": True,
        "format": output_format,
        "bytes": len(payload),
        "dimensions": [int(decoded.shape[1]), int(decoded.shape[0])],
        "channels": int(decoded.shape[2]),
        "mean_absolute_error": round(mean_error, 4),
        "maximum_mean_absolute_error": limit,
        "psnr_db": None if mean_squared == 0 else round(float(10 * np.log10(255.0 ** 2 / mean_squared)), 2),
    }


def _fit_pixel_budget(image):
    """Reduce an oversized source to ENHANCEMENT_MAX_INPUT_PIXELS before enhancing.

    Uploads may be far larger than the pipeline can process in bounded time and
    memory (denoising alone is linear in pixels and holds several float copies).
    Within the budget the original pixel grid is untouched.
    """
    height, width = image.shape[:2]
    budget = int(settings.ENHANCEMENT_MAX_INPUT_PIXELS)
    metadata = {
        "maximum_input_pixels": budget,
        "original_dimensions": [int(width), int(height)],
        "reduced": False,
    }
    if budget <= 0 or width * height <= budget:
        return image, metadata
    scale = (budget / float(width * height)) ** 0.5
    reduced_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    metadata.update({"reduced": True, "working_dimensions": list(reduced_size)})
    return cv2.resize(image, reduced_size, interpolation=cv2.INTER_AREA), metadata


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


UNSAFE_FAILURE_REASON = "The image could not be enhanced safely."
TIME_LIMIT_FAILURE_REASON = (
    "Enhancement ran out of time before this image finished. You can start it again."
)


def _discard_stored_output(result, stored_name):
    """Remove an output file written for a result that did not complete."""
    if not stored_name:
        return
    try:
        result.enhanced_file.storage.delete(stored_name)
    except OSError:
        logger.exception("Could not remove the abandoned output %s.", stored_name)


class ResultInProgress(Exception):
    """Another task holds a live claim on this result."""

    def __init__(self, retry_after):
        super().__init__("Another worker is already enhancing this image.")
        self.retry_after = retry_after


CANCELLED_REASON = "Processing was cancelled by the user."


def _owns(result, token):
    return ImageEnhancementResult.objects.filter(pk=result.pk, claim_token=token).exists()


def _mark_result_failed(result, started, reason, token=None):
    if token is not None and not _owns(result, token):
        return  # another worker took this result over; its outcome is not ours to write
    result.status = EnhancementStatus.FAILED
    result.failure_reason = reason
    result.claim_token = ""
    result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
    result.finished_at = timezone.now()
    result.save(update_fields=[
        "status", "failure_reason", "claim_token", "processing_duration_ms", "finished_at", "updated_at",
    ])


def _mark_result_cancelled(result, token):
    if not _owns(result, token):
        return
    result.status = EnhancementStatus.CANCELLED
    result.failure_reason = CANCELLED_REASON
    result.claim_token = ""
    result.finished_at = timezone.now()
    result.save(update_fields=["status", "failure_reason", "claim_token", "finished_at", "updated_at"])


# How often a running enhancement renews its claim between checkpoints. None
# means a quarter of the lease, so a renewal or two lost to a database blip
# still leaves the claim alive. Tests lower it.
CLAIM_RENEWAL_INTERVAL_SECONDS = None


class ClaimRenewal:
    """Keep a claim alive while one long stage runs between checkpoints.

    Checkpoints refresh the claim between stages, but a single stage (a large
    image through a model on the CPU) can outlast the lease, and a duplicate
    would then take over live work. A background thread renews the claim while
    this execution runs. It stops when the execution ends, when the claim is no
    longer this execution's, and once the task time limit has passed: no
    supported execution runs longer, so a claim still held then belongs to a
    stuck worker and must be allowed to lapse. A worker that dies takes the
    thread with it, so its claim lapses as before.
    """

    def __init__(self, result_pk, token):
        self.result_pk = result_pk
        self.token = token
        self.started = time.monotonic()
        self.interval = CLAIM_RENEWAL_INTERVAL_SECONDS or settings.ENHANCEMENT_CLAIM_LEASE_SECONDS / 4
        self._stop = threading.Event()
        self._thread = None

    def renew(self):
        """Refresh the claim; False once there is nothing (or no longer a reason) to renew."""
        if time.monotonic() - self.started > settings.CELERY_TASK_TIME_LIMIT:
            return False
        now = timezone.now()
        return bool(ImageEnhancementResult.objects.filter(
            pk=self.result_pk, claim_token=self.token, status=EnhancementStatus.PROCESSING,
        ).update(claimed_at=now, updated_at=now))

    def _run(self):
        from django.db import connections

        try:
            while not self._stop.wait(self.interval):
                try:
                    if not self.renew():
                        return
                except TRANSIENT_INFRASTRUCTURE_ERRORS:
                    logger.warning("Could not renew the claim on result %s; trying again.", self.result_pk)
        finally:
            connections.close_all()  # this thread's own connections only

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, name=f"claim-renewal-{self.result_pk}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join()
        return False


def _resumes_own_claim(result, resume_token):
    """Whether the live claim is the one this task's previous execution held.

    Celery runs a retry only after the execution that asked for it has ended,
    and that execution hands its claim token to the retry. Nothing else proves
    the claim has no worker behind it: a retry *number* also rises when a task
    retries to wait for a claim it never held, so a duplicate delivery could
    otherwise take over work that is still running.
    """
    return bool(resume_token) and result.claim_token == resume_token


def _claim_result(result, token, owner=None, resume_token=""):
    """Take ownership of a result, or refuse while another worker holds it.

    A queue can deliver one task twice, and two tasks can target one result.
    The claim is a token plus a timestamp renewed while the work runs; a claim
    not renewed within ENHANCEMENT_CLAIM_LEASE_SECONDS belongs to a worker that
    died, and may be taken over. ``owner`` is ``(task id, attempt number)`` and
    is recorded for diagnosis only. ``resume_token`` is the claim this task's
    previous execution held: a retry resumes it at once instead of waiting.
    """
    lease_seconds = settings.ENHANCEMENT_CLAIM_LEASE_SECONDS
    lease = timedelta(seconds=lease_seconds)
    with transaction.atomic():
        result = (
            ImageEnhancementResult.objects.select_for_update()
            .select_related("preprocessing_result", "image", "owner")
            .get(pk=result.pk)
        )
        if result.status == EnhancementStatus.COMPLETED and result.enhanced_file:
            return result, False
        if result.status == EnhancementStatus.CANCELLED:
            return result, False
        now = timezone.now()
        if (
            result.status == EnhancementStatus.PROCESSING
            and result.claim_token
            and result.claimed_at
            and now - result.claimed_at < lease
            and not _resumes_own_claim(result, resume_token)
        ):
            remaining = lease - (now - result.claimed_at)
            # Look again when the claim would lapse: never later than one lease.
            raise ResultInProgress(min(lease_seconds, max(1, math.ceil(remaining.total_seconds()))))
        result.status = EnhancementStatus.PROCESSING
        result.claim_token = token
        result.claimed_at = now
        result.claim_task_id, result.claim_attempt = owner or ("", 0)
        result.started_at = result.started_at or now
        result.finished_at = None
        result.failure_reason = ""
        result.save(update_fields=[
            "status", "claim_token", "claimed_at", "claim_task_id", "claim_attempt",
            "started_at", "finished_at", "failure_reason", "updated_at",
        ])
    return result, True


def process_enhancement_result(result, claim_owner=None, token=None, resume_token=""):
    """Process one result and persist a separate enhanced output.

    Safe to call twice for the same result, at the same time or one after the
    other: only the holder of the claim enhances, and only the holder publishes.
    ``token`` is the claim token to use (the caller keeps it, to tell its own
    claim from another worker's); ``resume_token`` is the claim the caller's
    previous, ended execution held, which it may resume. ``claim_owner`` is the
    calling task's ``(id, attempt number)``, recorded for diagnosis.
    """
    token = token or uuid.uuid4().hex
    result, claimed = _claim_result(result, token, claim_owner, resume_token)
    if not claimed:
        return result
    with ClaimRenewal(result.pk, token):
        return _process_claimed_result(result, token)


def _process_claimed_result(result, token):
    batch_id = result.image.batch_id

    def checkpoint(stage_name):
        """Refresh the claim; stop if it was lost or the batch was cancelled."""
        still_ours = ImageEnhancementResult.objects.filter(
            pk=result.pk, claim_token=token, status=EnhancementStatus.PROCESSING,
        ).update(claimed_at=timezone.now())
        if not still_ours:
            raise EnhancementSuperseded()
        if UploadBatch.objects.filter(pk=batch_id, cancellation_requested=True).exists():
            raise EnhancementCancelled()

    started = time.perf_counter()
    stored_name = None
    try:
        keep_alpha = result.preprocessing_result.output_format == "png"
        decoding = {}
        if keep_alpha:
            source, alpha, source_strategy = _read_enhancement_source_with_alpha(result, decoding)
        else:
            source, source_strategy = _read_enhancement_source(result, decoding)
            alpha = None
        source, pixel_budget = _fit_pixel_budget(source)
        if pixel_budget["reduced"]:
            source_strategy = "original_reduced_to_pixel_budget"
        from privacy.services import biometric_processing_allowed
        enhanced, applied_level, parameters = enhance_bgr(
            source, result.requested_level, style=result.requested_style,
            allow_biometric_processing=biometric_processing_allowed(result.owner),
            checkpoint=checkpoint,
        )
        parameters["source_resolution_strategy"] = source_strategy
        if decoding:
            parameters["source_decoding"] = decoding
            if decoding["colour_profile"].get("error"):
                parameters["warnings"].append(decoding["colour_profile"]["error"])
        parameters["input_pixel_budget"] = pixel_budget
        if pixel_budget["reduced"]:
            parameters["warnings"].append(
                "The original is larger than the enhancement pixel budget, so it "
                "was enhanced at a reduced size. The original file is unchanged."
            )
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
        parameters["transparency"] = {"source_had_alpha": alpha is not None, "preserved": alpha is not None}
        output_image = _attach_alpha(enhanced, alpha) if alpha is not None else enhanced
        payload, extension, output_format = _encode_output(output_image, result.preprocessing_result)
        parameters["encoded_output"] = _verify_encoded_output(payload, output_image, output_format)
        source_stem = Path(result.preprocessing_result.output_name or result.image.original_name).stem
        output_name = f"{source_stem}_enhanced_{applied_level}{extension}"
        # Last look before anything is written: a cancelled or taken-over
        # result must not gain an output file.
        checkpoint("publish")
        result.enhanced_file.save(output_name, ContentFile(payload), save=False)
        stored_name = result.enhanced_file.name
        result.applied_level = applied_level
        result.status = EnhancementStatus.COMPLETED
        result.claim_token = ""
        result.output_name = output_name
        result.output_format = output_format
        result.output_width = int(enhanced.shape[1])
        result.output_height = int(enhanced.shape[0])
        result.output_file_size = len(payload)
        result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        result.applied_parameters = parameters
        result.failure_reason = ""
        result.finished_at = timezone.now()
        with transaction.atomic():
            # Publish only as the owner, under the row lock, so two workers can
            # never both write a completed row.
            current = ImageEnhancementResult.objects.select_for_update().only(
                "status", "claim_token",
            ).get(pk=result.pk)
            if current.claim_token != token or current.status != EnhancementStatus.PROCESSING:
                raise EnhancementSuperseded()
            result.save()
        return result
    except EnhancementSuperseded:
        logger.warning("Result %s was taken over or closed by another worker; this output was discarded.", result.pk)
        _discard_stored_output(result, stored_name)
        raise
    except EnhancementCancelled:
        _discard_stored_output(result, stored_name)
        _mark_result_cancelled(result, token)
        raise
    except TRANSIENT_INFRASTRUCTURE_ERRORS:
        # The database went away; the row cannot be updated now. The task retries,
        # and its last attempt records the failure.
        _discard_stored_output(result, stored_name)
        raise
    except SoftTimeLimitExceeded:
        _discard_stored_output(result, stored_name)
        _mark_result_failed(result, started, TIME_LIMIT_FAILURE_REASON, token)
        raise
    except ValidationError:
        _discard_stored_output(result, stored_name)
        _mark_result_failed(result, started, UNSAFE_FAILURE_REASON, token)
        raise
    except Exception as error:
        # Model runtimes fail in ways no fixed list covers (an out-of-memory
        # RuntimeError, a MemoryError, a TypeError from a bad checkpoint). The
        # user sees one safe sentence; the cause goes to the log.
        logger.exception("Enhancement of result %s failed.", result.pk)
        _discard_stored_output(result, stored_name)
        _mark_result_failed(result, started, UNSAFE_FAILURE_REASON, token)
        raise ValidationError(UNSAFE_FAILURE_REASON) from error


EDITOR_CONTROL_NAMES = CONTROL_NAMES


def _auto_editor_adjustments(quality, intent="auto", blockiness_detected=False):
    """The control values of the local recipe (see controls.recommend_local_adjustments)."""
    return recommend_local_adjustments(quality, intent, blockiness_detected=blockiness_detected)["controls"]


def _render_editor_controls(source, controls, strength=1.0):
    """Apply bounded tone controls in float LAB (enhancement/tone.py)."""
    return render_tone(source, controls, strength)


def apply_gallery_adjustments(source, mode, requested_adjustments=None, baseline=None):
    """Render one non-destructive Auto or Manual gallery edit.

    ``baseline`` is the pre-AI-enhancement image (the preprocessed original).
    When provided and ``intensity`` < 100, the final render is blended back
    toward it -- this is the "how much AI enhancement" slider: 100 is the
    full enhanced result (the default, matching prior behaviour), 0 is the
    unenhanced original.

    Auto uses the local recipe (``requested_adjustments["intent"]`` may be
    "auto", "preserve" or "brighten"); Manual uses the validated control values.
    The returned metadata records what was requested, validated and applied,
    each guard reduction, any fallback and the renderer version.
    """
    if source is None or source.size == 0:
        raise ValidationError("The enhanced image cannot be edited safely.")
    if mode not in EditMode.values:
        raise ValidationError({"mode": "Choose auto or manual."})

    requested_raw = dict(requested_adjustments or {})
    before = analyze_image_quality(source)
    recommendation = None
    if mode == EditMode.AUTO:
        intent = requested_raw.get("intent", "auto")
        if intent not in INTENTS:
            raise ValidationError({"intent": f"Choose one of: {', '.join(INTENTS)}."})
        recommendation = recommend_local_adjustments(
            before, intent, blockiness_detected=measure_compression_artifacts(source)["detected"],
        )
        controls = recommendation["controls"]
        intensity = 100.0
    else:
        validated = validate_controls({key: value for key, value in requested_raw.items() if key != "intent"})
        intensity = validated.pop("intensity")
        controls = validated

    fallback_reasons = []
    strength = 1.0
    clipping_limit = max(
        settings.ENHANCEMENT_MAX_HIGHLIGHT_CLIPPING_PERCENT,
        before["highlight_clipping_percent"] + 2.0,
    )
    reasons = []
    guard_reductions = []
    rendered, effective = source.copy(), {name: 0.0 for name in CONTROL_NAMES}
    after = before
    if not is_neutral(controls):
        for _attempt in range(10):
            rendered, effective = _render_editor_controls(source, controls, strength)
            after = analyze_image_quality(rendered)
            contrast_floor = before["contrast_stddev"] * 0.70
            if (
                after["highlight_clipping_percent"] <= clipping_limit
                and after["contrast_stddev"] >= contrast_floor
            ):
                break
            attempt_reasons = []
            if after["highlight_clipping_percent"] > clipping_limit:
                attempt_reasons.append("highlight_clipping_limited")
            if after["contrast_stddev"] < contrast_floor:
                attempt_reasons.append("contrast_loss_limited")
            for reason in attempt_reasons:
                if reason not in reasons:
                    reasons.append(reason)
            guard_reductions.append({"strength": round(strength, 4), "reasons": attempt_reasons})
            strength *= 0.78

    intensity_applied = False
    if intensity < 100.0:
        if baseline is None:
            fallback_reasons.append("intensity_not_applied_no_unenhanced_baseline")
        else:
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

    unchanged = np.array_equal(rendered, source)
    metadata = {
        "mode": mode,
        "policy": RENDERER_POLICY_NAME,
        "renderer_version": RENDERER_VERSION,
        "render_policy": rendering_policy.active_policy(),
        "outcome": "no_change" if unchanged else "rendered",
        "requested": controls,
        "validated": {**controls, "intensity": round(intensity, 2)},
        "effective": {name: round(value, 3) for name, value in effective.items()},
        "applied": {name: round(value, 3) for name, value in effective.items()},
        "safety_strength": round(strength, 4),
        "safety_reasons": reasons,
        "guard_reductions": guard_reductions,
        "fallback_reasons": fallback_reasons,
        "quality_before": before,
        "quality_after": after,
        "dimensions_preserved": source.shape[:2] == rendered.shape[:2],
        "intensity": {
            "requested": round(intensity, 2),
            "applied": intensity_applied,
            "policy": "blend_toward_preprocessed_original",
        },
    }
    if recommendation is not None:
        metadata["auto"] = {key: recommendation[key] for key in ("action", "intent", "rationale", "caution_flags")}
    return rendered, metadata


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


class EditSuperseded(Exception):
    """The edit was deleted (cancelled) or taken over while it rendered; nothing is published."""


class EditInProgress(Exception):
    """Another execution holds a live claim on this edit."""


def process_image_edit(edit, token=None, resume_token=""):
    """Render and persist a gallery edit as a separate immutable file version.

    Only the execution holding the claim renders, and only it publishes, under
    the row lock: a second delivery of the same task returns without rendering,
    and an edit deleted (cancelled) while it renders is never brought back.
    ``token`` is this execution's claim; ``resume_token`` is the claim the
    caller's previous, ended attempt held, which it may resume.

    The edit is rendered from the enhanced image it was made from
    (``source_version``), at full resolution, in one pass, and keeps its
    transparency (see editor_source.py).
    """
    token = token or uuid.uuid4().hex
    lease = timedelta(seconds=settings.ENHANCEMENT_CLAIM_LEASE_SECONDS)
    with transaction.atomic():
        locked = (
            ImageEditVersion.objects.select_for_update()
            .select_related("enhancement_result__preprocessing_result", "owner")
            .filter(pk=edit.pk).first()
        )
        if locked is None:
            raise EditSuperseded()
        edit = locked
        if edit.status == EnhancementStatus.COMPLETED and edit.edited_file:
            return edit
        if edit.status in (EnhancementStatus.CANCELLED, EnhancementStatus.FAILED) and edit.finished_at:
            return edit
        now = timezone.now()
        if (
            edit.status == EnhancementStatus.PROCESSING and edit.claim_token and edit.claimed_at
            and now - edit.claimed_at < lease and not (resume_token and edit.claim_token == resume_token)
        ):
            raise EditInProgress()
        current_version = source_version(edit.enhancement_result)
        if edit.source_version and edit.source_version != current_version:
            edit.status = EnhancementStatus.FAILED
            edit.failure_reason = "The image changed after this edit was requested. Start the edit again."
            edit.claim_token = ""
            edit.finished_at = now
            edit.save(update_fields=["status", "failure_reason", "claim_token", "finished_at", "updated_at"])
            return edit
        edit.source_version = edit.source_version or current_version
        edit.status = EnhancementStatus.PROCESSING
        edit.claim_token = token
        edit.claimed_at = now
        edit.started_at = edit.started_at or now
        edit.failure_reason = ""
        edit.save(update_fields=[
            "status", "claim_token", "claimed_at", "started_at", "failure_reason", "source_version", "updated_at",
        ])

    started = time.perf_counter()
    stored_name = None
    try:
        result = edit.enhancement_result
        preprocessing = result.preprocessing_result
        source, alpha = read_enhanced_with_alpha(result)
        baseline = _read_preprocessed_baseline(preprocessing)
        requested = dict(edit.requested_adjustments or {})
        rendered, metadata = apply_gallery_adjustments(
            visible_for_measurement(source, alpha), edit.mode, requested, baseline=baseline,
        )
        rendered = restore_hidden(rendered, source, alpha)
        output_image = attach(rendered, alpha)
        payload, extension, output_format = _encode_output(output_image, preprocessing)
        metadata["encoded_output"] = _verify_encoded_output(payload, output_image, output_format)
        metadata["transparency"] = {
            "source_had_alpha": alpha is not None, "preserved": alpha is not None,
            "policy": transparency_policy(output_format),
        }
        metadata["source_version"] = edit.source_version
        source_stem = Path(result.output_name).stem
        output_name = f"{source_stem}_edit_{edit.pk}{extension}"
        edit.edited_file.save(output_name, ContentFile(payload), save=False)
        stored_name = edit.edited_file.name
        duration = max(1, round((time.perf_counter() - started) * 1000))
        with transaction.atomic():
            # Publish only as the owner, under the row lock, from the row as it is now.
            current = ImageEditVersion.objects.select_for_update().filter(pk=edit.pk).first()
            if (
                current is None or current.claim_token != token
                or current.status != EnhancementStatus.PROCESSING
            ):
                raise EditSuperseded()
            current.edited_file.name = stored_name
            current.status = EnhancementStatus.COMPLETED
            current.claim_token = ""
            current.applied_adjustments = metadata["effective"]
            current.output_name = output_name
            current.output_format = output_format
            current.output_width = int(rendered.shape[1])
            current.output_height = int(rendered.shape[0])
            current.output_file_size = len(payload)
            current.processing_duration_ms = duration
            current.quality_metadata = metadata
            current.failure_reason = ""
            current.finished_at = timezone.now()
            ImageEditVersion.objects.filter(
                enhancement_result=current.enhancement_result_id, is_current=True,
            ).exclude(pk=current.pk).update(is_current=False)
            current.is_current = True
            current.save()
        return current
    except EditSuperseded:
        logger.warning("Edit %s was deleted or taken over while it rendered; its output was discarded.", edit.pk)
        if stored_name:
            edit.edited_file.storage.delete(stored_name)
        raise
    except TRANSIENT_INFRASTRUCTURE_ERRORS:
        if stored_name:
            edit.edited_file.storage.delete(stored_name)
        raise  # the task retries with this claim; its last attempt records the failure
    except Exception as error:
        if stored_name:
            edit.edited_file.storage.delete(stored_name)
        failed = ImageEditVersion.objects.filter(pk=edit.pk, claim_token=token).update(
            status=EnhancementStatus.FAILED, claim_token="", finished_at=timezone.now(),
            failure_reason="The gallery edit could not be rendered safely.",
            processing_duration_ms=max(1, round((time.perf_counter() - started) * 1000)),
        )
        if not failed:
            raise EditSuperseded() from error
        if isinstance(error, ValidationError):
            raise
        logger.exception("Gallery edit %s failed.", edit.pk)
        raise ValidationError("The gallery edit could not be rendered safely.") from error



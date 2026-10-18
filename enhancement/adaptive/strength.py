"""How hard each stage works, as a continuous function of what was measured on THIS photograph.

On the automatic path no stage has a fixed recipe: a slightly dim photograph gets a gentle lift and a nearly black one the full lift, mild
noise a light denoise and heavy grain a strong one, a barely soft face a high-fidelity restoration and a ruined one a bold one. Each function
returns the strength together with the measurement it came from, so the stored plan says *why* this photograph was treated this way.

Every anchor point is a documented setting (``ADAPTIVE_*``, docs/deployment/restoration-chain.md). A strength is a blend between the
original and the model's output (1.0 = the model's full result) except face fidelity, which is the restorer's own ``w`` (higher keeps more
of the input). The validator still judges every candidate; a strength is where to start, not permission.
"""
import math
from dataclasses import dataclass, replace

from django.conf import settings


def _clip(value, low, high):
    return max(low, min(high, value))


def _lerp(low, high, fraction):
    return low + (high - low) * _clip(fraction, 0.0, 1.0)


@dataclass(frozen=True)
class StrengthConfig:
    lowlight_target_median: float = 110.0
    lowlight_blend_min: float = 0.45
    denoise_blend_min: float = 0.35
    deblur_blend_min: float = 0.5
    face_fidelity_min: float = 0.5
    face_fidelity_max: float = 0.9
    tone_contrast_target: float = 55.0
    tone_strength_min: float = 0.25
    tone_strength_max: float = 0.8
    face_detail_poor: float = 50.0
    face_detail_acceptable: float = 75.0
    style_gain: float = 1.0   # the unsharp gain of the tone pass, raised by the "dynamic" and "ai_max" styles

    @classmethod
    def from_settings(cls, style="natural"):
        """The anchors from settings, then biased by the style: ``natural`` as measured, ``dynamic`` punchier tone and detail,
        ``ai_max`` the strongest restoration (higher blends, bolder face restoration)."""
        base = cls._from_settings()
        style = str(style)
        if style == "dynamic":
            return replace(base, tone_strength_min=min(1.0, base.tone_strength_min * 1.4), tone_strength_max=min(1.0, base.tone_strength_max * 1.25),
                           style_gain=1.3)
        if style == "ai_max":
            return replace(base, lowlight_blend_min=min(1.0, base.lowlight_blend_min + 0.1), denoise_blend_min=min(1.0, base.denoise_blend_min + 0.1),
                           deblur_blend_min=min(1.0, base.deblur_blend_min + 0.15), face_fidelity_max=max(base.face_fidelity_min, base.face_fidelity_max - 0.1),
                           face_fidelity_min=max(0.3, base.face_fidelity_min - 0.1), tone_strength_min=min(1.0, base.tone_strength_min * 1.15),
                           style_gain=1.15)
        return base

    @classmethod
    def _from_settings(cls):
        return cls(
            lowlight_target_median=float(settings.ADAPTIVE_LOWLIGHT_TARGET_MEDIAN), lowlight_blend_min=float(settings.ADAPTIVE_LOWLIGHT_BLEND_MIN),
            denoise_blend_min=float(settings.ADAPTIVE_DENOISE_BLEND_MIN), deblur_blend_min=float(settings.ADAPTIVE_DEBLUR_BLEND_MIN),
            face_fidelity_min=float(settings.ADAPTIVE_FACE_FIDELITY_MIN), face_fidelity_max=float(settings.ADAPTIVE_FACE_FIDELITY_MAX),
            tone_contrast_target=float(settings.ADAPTIVE_TONE_CONTRAST_TARGET), tone_strength_min=float(settings.ADAPTIVE_TONE_STRENGTH_MIN),
            tone_strength_max=float(settings.ADAPTIVE_TONE_STRENGTH_MAX),
        )


def lowlight(report, c):
    """Lift: from how far the face (or, with no face, the frame) sits below the target median. Returns ``(blend, measure)``."""
    main = report.main_face
    if report.exposure.get("judged_on") == "face" and main is not None and main.luminance.get("p50") is not None:
        median, judged_on = float(main.luminance["p50"]), "face"
    else:
        median, judged_on = float(report.exposure["scene_median"]), "frame"
    deficit = _clip((c.lowlight_target_median - median) / c.lowlight_target_median, 0.0, 1.0)
    # The classical engine's gamma is the exponent that maps this photograph's median onto the target (a learned engine ignores it).
    gamma = _clip(math.log(c.lowlight_target_median / 255.0) / math.log(max(median, 1.0) / 255.0), 0.35, 0.9)
    return round(_lerp(c.lowlight_blend_min, 1.0, deficit), 3), {"judged_on": judged_on, "median": round(median, 1),
                                                               "target_median": c.lowlight_target_median, "deficit": round(deficit, 3),
                                                               "classical_gamma": round(gamma, 3)}


def denoise(report, c):
    """Denoise: from the noise level (1.0 is "moderate", about 2.5 is heavy grain)."""
    level = float(report.noise.get("level", 1.0))
    severity = _clip((level - 0.75) / 1.75, 0.0, 1.0)
    return round(_lerp(c.denoise_blend_min, 1.0, severity), 3), {"noise_level": round(level, 3), "noise_status": report.noise.get("status")}


def face_severity(report, c):
    """0 for a face with plenty of detail, 1 for a face with none (detail measured at 128 px; sharp is about 100 or more)."""
    detail = report.face_blur.get("detail")
    if detail is None:
        return None
    return _clip(1.0 - detail / c.face_detail_poor, 0.0, 1.0)


def deblur(report, c):
    """Deblur: from the face's own blur when the face triggered it, else from how much of the frame's fine detail survives an extra blur."""
    severity = face_severity(report, c) if report.face_blur.get("status") == "high" else None
    if severity is not None:
        return round(_lerp(c.deblur_blend_min, 1.0, severity), 3), {"judged_on": "face", "face_detail": report.face_blur["detail"], "severity": round(severity, 3)}
    retention = report.blur.get("retention")
    severity = _clip(((retention if retention is not None else 0.6) - 0.5) / 0.4, 0.0, 1.0)
    return round(_lerp(c.deblur_blend_min, 1.0, severity), 3), {"judged_on": "frame", "retention": retention, "blur_type": report.blur.get("type"),
                                                              "severity": round(severity, 3)}


def face_fidelity(report, c):
    """Face restoration ``w``: bolder (lower) the less detail the face has, closer to the input (higher) the softer-only it is."""
    main = report.main_face
    detail = None if main is None else main.detail
    if detail is None:
        return round(_lerp(c.face_fidelity_max, c.face_fidelity_min, 0.5), 3), {"face_detail": None, "severity": None}
    severity = _clip(1.0 - detail / c.face_detail_acceptable, 0.0, 1.0)
    return round(_lerp(c.face_fidelity_max, c.face_fidelity_min, severity), 3), {"face_detail": detail, "severity": round(severity, 3)}


def tone(report, c):
    """Tone and detail: gentle on a photograph that already has contrast, firmer on a flat one, never below the configured minimum."""
    contrast = float(report.base.get("contrast_stddev", c.tone_contrast_target))
    deficit = _clip((c.tone_contrast_target - contrast) / c.tone_contrast_target, 0.0, 1.0)
    return round(_lerp(c.tone_strength_min, c.tone_strength_max, deficit), 3), {"contrast": round(contrast, 1), "target_contrast": c.tone_contrast_target,
                                                                              "deficit": round(deficit, 3)}


def upscale_scale(long_edge, target, max_edge):
    """The smallest useful scale to reach ``target`` on the long edge: 2, or 4 only when 2 falls short; bounded by ``max_edge``.

    Returns ``(scale, reached, reason)``; ``scale`` is 0 when no enlargement is wanted or possible.
    """
    if long_edge >= target:
        return 0, True, f"The photograph's long edge ({long_edge} px) already reaches the HD target ({target} px)."
    for scale in (2, 4):
        if long_edge * scale >= target:
            if long_edge * scale <= max_edge:
                return scale, True, f"x{scale} takes the long edge from {long_edge} to {long_edge * scale} px (HD target {target})."
            break
    # x2 does not reach the target (or the smallest that does is over the limit): the largest scale the limit allows, which may not reach it.
    for scale in (4, 2):
        if long_edge * scale <= max_edge:
            return scale, long_edge * scale >= target, (
                f"x{scale} takes the long edge from {long_edge} to {long_edge * scale} px; the HD target ({target}) "
                + ("is reached." if long_edge * scale >= target else "is not reached with one enlargement."))
    return 0, False, f"Enlarging would exceed the output limit ({max_edge} px)."

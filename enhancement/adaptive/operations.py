"""The operations the engine can propose, each wrapping one real model adapter (or its classical fallback).

Every function takes the current image and returns ``(candidate_image, models)`` where ``models`` lists what actually
ran (name, checkpoint prefix, device, seconds), so the record says which engine produced a candidate. A model that
cannot run raises its adapter's "unavailable" error; the engine records the reason and tries the next engine or skips
the operation. Nothing here reports a model it did not run: a classical fallback names itself as one.

Variants are blends of the model's output with its input (``blend`` 1.0 is the model's full result). A smaller blend is
the conservative candidate the validator can prefer when the full one over-reaches.
"""
import time

import cv2
import numpy as np
from django.conf import settings

from .colour import chromaticity_linear

# Operation names, in the order the engine considers them (low light first: it changes what the rest see).
LOW_LIGHT = "low_light_enhancement"
DENOISE = "denoising"
DEBLUR = "motion_deblurring"
FACE = "face_restoration"
UPSCALE = "super_resolution"
TONE = "tone_detail"   # a gentle local-contrast and detail pass; only on the automatic path (the router asks for it there)
ORDER = (LOW_LIGHT, DENOISE, DEBLUR, FACE, TONE, UPSCALE)

ENGINES = {
    LOW_LIGHT: ("retinexformer", "zero_dce", "opencv_exposure"),
    DENOISE: ("nafnet_sidd", "opencv_nlm"),
    DEBLUR: ("nafnet_gopro",),
    FACE: ("codeformer", "gfpgan"),
    UPSCALE: ("realesrgan", "lanczos"),   # lanczos: a conventional filter, only when Real-ESRGAN cannot run (ADAPTIVE_HD_FALLBACK_LANCZOS)
    TONE: ("opencv_tone",),
}


def _blend(parent, candidate, blend):
    if blend >= 0.999:
        return candidate
    return cv2.addWeighted(candidate, blend, parent, 1.0 - blend, 0)


def _flags():
    """Which engines are switched on (the model's own setting; the adaptive policy allows every learned stage)."""
    return {
        "retinexformer": bool(settings.RETINEXFORMER_ENABLED), "zero_dce": bool(settings.ZERO_DCE_ENABLED),
        "opencv_exposure": True, "nafnet_sidd": bool(settings.NAFNET_DENOISE_ENABLED), "opencv_nlm": True,
        "nafnet_gopro": bool(settings.NAFNET_DEBLUR_ENABLED), "codeformer": bool(settings.CODEFORMER_ENABLED),
        "gfpgan": bool(settings.GFPGAN_ENABLED), "realesrgan": bool(settings.HD_RESTORATION_ENABLED),
        "lanczos": bool(settings.ADAPTIVE_HD_FALLBACK_LANCZOS), "opencv_tone": True,
    }


def enabled_engines(operation):
    flags = _flags()
    return [engine for engine in ENGINES[operation] if flags[engine]]


def _record(name, metadata, seconds, **extra):
    return {"model": name, "checkpoint": metadata.get("checkpoint_sha256_prefix"), "device": metadata.get("device"),
            "provider": metadata.get("provider"), "seconds": round(seconds, 3), **extra}


def run_low_light(image, engine, blend, gamma=None):
    from enhancement import services

    started = time.perf_counter()
    if engine == "retinexformer":
        from enhancement.lowlight import enhance_with_retinexformer

        raw, metadata = enhance_with_retinexformer(image)
    elif engine == "zero_dce":
        from enhancement.zero_dce import enhance_with_zero_dce

        raw, metadata = enhance_with_zero_dce(image)
    else:  # the classical exposure correction: gamma on LAB lightness with a conservative blend
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        lightness = lab[:, :, 0].astype(np.float32) / 255.0
        exponent = 0.6 if gamma is None else float(gamma)   # the automatic path derives it from the measured median
        lab[:, :, 0] = np.clip(255.0 * np.power(lightness, exponent), 0, 255).astype(np.uint8)
        raw, metadata = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), {"provider": f"opencv_gamma_{exponent:.2f}"}
    # The network shifts colour (it returns paler, hue-shifted output); the photograph's own colour is kept and only its brightness
    # is taken from the network. "legacy" is the earlier, measurably paler method, kept for comparison and rollback.
    if settings.ADAPTIVE_LOWLIGHT_COLOUR == "legacy":
        protected = services._preserve_source_chroma(image, raw)
        raw, chroma = services._amplify_source_chroma(image, protected, settings.ENHANCEMENT_DARK_COLOUR_GAIN_MAX)
        chroma["method"] = "legacy_lab_chroma_sqrt_gain"
    else:
        raw, chroma = chromaticity_linear(image, raw), {"method": "chromaticity_linear"}
    seconds = time.perf_counter() - started
    return _blend(image, raw, blend), [_record(engine, metadata, seconds, chroma=chroma)]


def run_denoise(image, engine, blend):
    started = time.perf_counter()
    if engine == "nafnet_sidd":
        from enhancement.hd_restoration import denoise_with_nafnet

        raw, metadata = denoise_with_nafnet(image)
    else:
        from enhancement import services

        raw, metadata = services._edge_aware_denoise(image, 10), {"provider": "edge_aware_nlm"}
    seconds = time.perf_counter() - started
    return _blend(image, raw, blend), [_record(engine, metadata, seconds)]


def run_deblur(image, engine, blend):
    from enhancement.hd_restoration import deblur_with_nafnet

    started = time.perf_counter()
    raw, metadata = deblur_with_nafnet(image)
    return _blend(image, raw, blend), [_record(engine, metadata, time.perf_counter() - started)]


def run_face_restoration(image, engine, fidelity):
    """CodeFormer (``fidelity`` is its w: higher keeps more of the input) or GFPGAN; faces only, pasted back feathered."""
    started = time.perf_counter()
    if engine == "codeformer":
        from enhancement.codeformer import restore_faces_codeformer

        raw, metadata = restore_faces_codeformer(image, fidelity=fidelity)
    else:
        from enhancement.gfpgan import restore_faces

        raw, metadata = restore_faces(image)
    return raw, [_record(engine, metadata, time.perf_counter() - started, fidelity=fidelity,
                         faces_restored=metadata.get("faces_restored"))]


def run_tone(image, strength, style_gain=1.0):
    """A gentle, halo-free tone and detail pass on luminance only; colour is untouched.

    Detail: the difference between the luminance and its edge-preserving (bilateral) base is boosted, so edges are not outlined the way an
    ordinary unsharp mask does. Tone: a mild S-curve around the mid-tones that returns to black and white at the ends. ``strength``
    (0 to 1, from the photograph's own contrast) sets both, bounded: at the top the detail gain is 0.55 and the mid-tone slope 1.35.
    """
    started = time.perf_counter()
    strength = min(max(float(strength), 0.0), 1.0)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness = lab[:, :, 0]
    base = cv2.bilateralFilter(lightness, 9, 18, 5).astype(np.float32)
    amount = min(0.55, (0.12 + 0.43 * strength) * style_gain)
    boosted = (lightness.astype(np.float32) + amount * (lightness.astype(np.float32) - base)) / 255.0
    # The S-curve pivots on the photograph's own median (not on mid-grey), so a dim photograph is not darkened in its shadows, and it
    # returns to black and white at the ends. Its slope at the pivot is at most 1.35 and it is monotonic.
    pivot = float(min(max(np.median(lightness) / 255.0, 0.2), 0.8))
    slope = 0.35 * strength
    reach = np.where(boosted < pivot, pivot, 1.0 - pivot)
    t = np.clip((boosted - pivot) / reach, -1.0, 1.0)
    curved = boosted + slope * min(pivot, 1.0 - pivot) * t * (1.0 - t * t)
    lab[:, :, 0] = np.clip(np.rint(curved * 255.0), 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), [_record("opencv_tone", {"provider": "opencv_bilateral_detail_scurve"}, time.perf_counter() - started,
                                                          strength=round(strength, 3), detail_gain=round(amount, 3), midtone_slope=round(1.0 + slope, 3))]


def run_upscale(image, engine, scale):
    if engine == "lanczos":
        started = time.perf_counter()
        enlarged = cv2.resize(image, (image.shape[1] * scale, image.shape[0] * scale), interpolation=cv2.INTER_LANCZOS4)
        return enlarged, [_record("lanczos", {"provider": "opencv_lanczos4"}, time.perf_counter() - started, scale=scale,
                                  note="A conventional enlargement: no detail is added.")]
    from enhancement.hd_restoration import upscale_with_realesrgan

    started = time.perf_counter()
    raw, metadata = upscale_with_realesrgan(image, output_scale=scale)
    blend = min(max(float(settings.REALESRGAN_OUTPUT_BLEND), 0.0), 1.0)
    reference = cv2.resize(image, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_CUBIC)
    raw = cv2.addWeighted(raw, blend, reference, 1.0 - blend, 0)
    return raw, [_record(engine, metadata, time.perf_counter() - started, scale=scale, candidate_blend=blend)]

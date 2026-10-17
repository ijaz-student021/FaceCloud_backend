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
ORDER = (LOW_LIGHT, DENOISE, DEBLUR, FACE, UPSCALE)

ENGINES = {
    LOW_LIGHT: ("retinexformer", "zero_dce", "opencv_exposure"),
    DENOISE: ("nafnet_sidd", "opencv_nlm"),
    DEBLUR: ("nafnet_gopro",),
    FACE: ("codeformer", "gfpgan"),
    UPSCALE: ("realesrgan",),
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
    }


def enabled_engines(operation):
    flags = _flags()
    return [engine for engine in ENGINES[operation] if flags[engine]]


def _record(name, metadata, seconds, **extra):
    return {"model": name, "checkpoint": metadata.get("checkpoint_sha256_prefix"), "device": metadata.get("device"),
            "provider": metadata.get("provider"), "seconds": round(seconds, 3), **extra}


def run_low_light(image, engine, blend):
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
        lab[:, :, 0] = np.clip(255.0 * np.power(lightness, 0.6), 0, 255).astype(np.uint8)
        raw, metadata = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), {"provider": "opencv_gamma_0_6"}
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


def run_upscale(image, engine, scale):
    from enhancement.hd_restoration import upscale_with_realesrgan

    started = time.perf_counter()
    raw, metadata = upscale_with_realesrgan(image, output_scale=scale)
    blend = min(max(float(settings.REALESRGAN_OUTPUT_BLEND), 0.0), 1.0)
    reference = cv2.resize(image, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_CUBIC)
    raw = cv2.addWeighted(raw, blend, reference, 1.0 - blend, 0)
    return raw, [_record(engine, metadata, time.perf_counter() - started, scale=scale, candidate_blend=blend)]

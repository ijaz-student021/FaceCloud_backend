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

# Operation names, in the order the engine considers them (low light first: it changes what the rest see; polish last: the router
# only offers it for a photograph on which nothing else was found, tried or applied).
LOW_LIGHT = "low_light_enhancement"
DENOISE = "denoising"
DEBLUR = "motion_deblurring"
FACE = "face_restoration"
UPSCALE = "super_resolution"
DETAIL = "detail_restoration"   # clearer at the SAME size: a soft or heavily compressed photograph, run through a 4x model at 2x and shrunk back
POLISH = "polish"
COLORIZE = "colourization"    # last: it takes the lightness of the best black-and-white image the other operations produced
ORDER = (LOW_LIGHT, DENOISE, DEBLUR, FACE, UPSCALE, DETAIL, POLISH, COLORIZE)

ENGINES = {
    LOW_LIGHT: ("retinexformer", "zero_dce", "opencv_exposure"),
    DENOISE: ("nafnet_sidd", "opencv_nlm"),
    DEBLUR: ("nafnet_gopro",),
    FACE: ("codeformer", "gfpgan"),
    UPSCALE: ("realesrgan",),
    DETAIL: ("ultrasharp", "general", "realesrgan"),      # the router puts the compact "general" first unless the style/strength asks for the best (ultrasharp)
    POLISH: ("opencv_polish",),
    COLORIZE: ("ddcolor",),
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
        "opencv_polish": True,   # OpenCV and NumPy only; whether the router offers it at all is ADAPTIVE_POLISH_ENABLED
        "ddcolor": bool(settings.DDCOLOR_ENABLED), "ultrasharp": bool(settings.ULTRASHARP_ENABLED), "general": bool(settings.HD_RESTORATION_ENABLED),
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


_DETAIL_OUTPUT = {"image": None, "engine": None, "raw": None, "metadata": None}


def clear_detail_cache():
    _DETAIL_OUTPUT.update(image=None, engine=None, raw=None, metadata=None)


def _detail_model_output(image, engine):
    """The model's 2x output for ``image``, computed ONCE for all the blends of one step: the blends are cheap, the model is not (measured 70 s per run for a 2.2 MP photograph
    on a loaded machine; running it once per variant doubled the time of the whole operation)."""
    from enhancement import hd_restoration

    if _DETAIL_OUTPUT["image"] is image and _DETAIL_OUTPUT["engine"] == engine:
        return _DETAIL_OUTPUT["raw"], _DETAIL_OUTPUT["metadata"]
    upscale = {"ultrasharp": hd_restoration.upscale_with_ultrasharp, "general": hd_restoration.upscale_with_general}.get(engine, hd_restoration.upscale_with_realesrgan)
    raw, metadata = upscale(image, output_scale=2)
    _DETAIL_OUTPUT.update(image=image, engine=engine, raw=raw, metadata=metadata)
    return raw, metadata


def run_detail(image, engine, blend):
    """``image`` made clearer at its own size: the 4x model at 2x (4x-UltraSharp or Real-ESRGAN x4plus), blended with a cubic enlargement, shrunk back with INTER_AREA.

    ``blend`` (1.0 = the style's full strength) is scaled per engine to the strength each one measured as natural on the owner's soft 1600 x 1382 portrait: 4x-UltraSharp 1.0 (clean, no
    halos), Real-ESRGAN x4plus REALESRGAN_OUTPUT_BLEND (0.35, the strength the engine has always used; its plain output has halos), the compact "general" network 0.45 (at 0.6 the eyes and
    beard were slightly over-crisp, at 1.0 the face detail left the validator's limit).
    """
    started = time.perf_counter()
    raw, metadata = _detail_model_output(image, engine)
    share = {"ultrasharp": 1.0, "general": 0.45}.get(engine, float(settings.REALESRGAN_OUTPUT_BLEND))
    strength = min(1.0, max(0.0, float(blend) * share))
    reference = cv2.resize(image, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_CUBIC)
    mixed = raw if strength >= 0.999 else cv2.addWeighted(raw, strength, reference, 1.0 - strength, 0)
    restored = cv2.resize(mixed, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_AREA)
    return restored, [_record(engine, metadata, time.perf_counter() - started, blend=round(strength, 3), supersample=2)]


def run_colorize(image, engine, variant):
    """The black-and-white ``image`` colourized (lightness untouched, chroma predicted and damped: enhancement/colorize.py)."""
    from enhancement.colorize import colorize_with_ddcolor

    started = time.perf_counter()
    raw, metadata = colorize_with_ddcolor(image)
    return raw, [_record(engine, metadata, time.perf_counter() - started, chroma_scale=metadata.get("chroma_scale"))]


def run_upscale(image, engine, scale):
    from enhancement.hd_restoration import upscale_with_realesrgan

    started = time.perf_counter()
    raw, metadata = upscale_with_realesrgan(image, output_scale=scale)
    blend = min(max(float(settings.REALESRGAN_OUTPUT_BLEND), 0.0), 1.0)
    reference = cv2.resize(image, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_CUBIC)
    raw = cv2.addWeighted(raw, blend, reference, 1.0 - blend, 0)
    return raw, [_record(engine, metadata, time.perf_counter() - started, scale=scale, candidate_blend=blend)]


# -- polish: a gentle, bounded improvement for a photograph on which nothing else was found ------------------------------------------
# OpenCV and NumPy only (no model, no licence). It is offered by the router only when ADAPTIVE_POLISH_ENABLED is on, the analyzer
# recommends no other operation, none was tried, and the scene is not a dark one left as photographed; and its result is kept only if
# the validator's polish gates pass (a measurable gain, nothing worse, the same photograph). Steps, on CIELAB lightness first:
#   (a) local contrast: CLAHE (clip 1.5, 8x8 tiles) blended in by the style's share, keeping only its mid-scale band: the full CLAHE
#       change also lifts grain and JPEG blocks (measured: noise estimate x1.17 on two compressed portraits, landmark jitter 1.9%)
#       and compresses the global tone range; the band between sigma 4 and 32 (at the 1024 px working scale) does neither;
#   (b) a lightness-only unsharp mask (sigma 1.2 at the working scale) with a soft threshold so grain and flat regions are not
#       amplified, and a lower amount inside each face, none at all for a face that is already sharp;
#   (c) levels: only when the 1st-99th percentile of lightness uses under 85 percent of the range, a stretch of at most 8 percent
#       about the middle, never into clipping;
#   (d) vibrance: at most 6 percent more chroma, on low-chroma pixels only.
# Global decisions (the CLAHE band, the stretch) are made on a 1024 px copy and applied to the full image in overlapping strips, so the
# memory used does not grow with the photograph and the result does not depend on the strip size.
POLISH_STRENGTH = {"natural": (0.25, 0.25), "dynamic": (0.55, 0.50)}    # style -> (CLAHE share, unsharp amount); ai_max has none
_POLISH_CLAHE_CLIP, _POLISH_CLAHE_TILES = 1.5, 8
_POLISH_WORK_EDGE = 1024
_POLISH_BAND = (4.0, 32.0)             # sigmas of the CLAHE change that are kept (a band-pass), at the working scale
_POLISH_USM_SIGMA = 1.2                # at the working scale
_POLISH_USM_THRESHOLD = 1.2            # lightness units (0-100) of detail below which nothing is added
_POLISH_FACE_AMOUNT_RATIO = 0.2 / 0.35 # the amount inside a face relative to the rest of the frame
_POLISH_FACE_SHARP = (119.0, 175.0)    # face detail (face_sharpness.py): a sharp face's median, and its 90th percentile
_POLISH_LEVELS_SPAN, _POLISH_LEVELS_MAX = 85.0, 1.08
_POLISH_VIBRANCE_MAX, _POLISH_VIBRANCE_CHROMA = 0.06, 50.0
_POLISH_STRIP_ROWS, _POLISH_STRIP_PAD = 1024, 48


def _polish_lab(bgr):
    return cv2.cvtColor(bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)


def _polish_bgr(lab):
    return np.clip(np.rint(cv2.cvtColor(lab, cv2.COLOR_Lab2BGR) * 255.0), 0, 255).astype(np.uint8)


def _polish_amount_map(shape, faces, amount, y_offset):
    """The unsharp amount per pixel: ``amount`` everywhere, less inside a face (none when that face is already sharp)."""
    if not faces:
        return amount
    reduction = np.zeros(shape[:2], np.float32)
    for x, y, w, h, detail in faces:
        low, high = _POLISH_FACE_SHARP
        headroom = 1.0 if detail is None else float(np.clip((high - detail) / (high - low), 0.0, 1.0))
        mask = np.zeros(shape[:2], np.float32)
        cv2.ellipse(mask, (int(x + w / 2), int(y + h / 2 - y_offset)), (max(1, int(w * 0.58)), max(1, int(h * 0.58))), 0, 0, 360, 1.0, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), max(1.5, 0.12 * w))
        reduction = np.maximum(reduction, mask * (1.0 - _POLISH_FACE_AMOUNT_RATIO * headroom))
    return amount * (1.0 - reduction)


def _polish_sharpen(lightness, sigma, amount, faces, y_offset):
    detail = lightness - cv2.GaussianBlur(lightness, (0, 0), sigma)
    detail = np.sign(detail) * np.maximum(np.abs(detail) - _POLISH_USM_THRESHOLD, 0.0)
    return lightness + _polish_amount_map(lightness.shape, faces, amount, y_offset) * detail


def _polish_plan(image, clahe_share, amount, faces):
    """The global decisions, made on the 1024 px working copy: the CLAHE change map (band-passed) and the levels stretch."""
    height, width = image.shape[:2]
    scale = min(1.0, _POLISH_WORK_EDGE / max(height, width))
    small = image if scale >= 1.0 else cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    sigma_scale = min(1.0, max(small.shape[:2]) / _POLISH_WORK_EDGE)       # a small photograph keeps proportionate radii
    lightness = _polish_lab(small)[:, :, 0]
    quantised = np.clip(np.rint(lightness * 2.55), 0, 255).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=_POLISH_CLAHE_CLIP, tileGridSize=(_POLISH_CLAHE_TILES, _POLISH_CLAHE_TILES))
    change = (clahe.apply(quantised).astype(np.float32) - quantised.astype(np.float32)) / 2.55 * clahe_share
    narrow, wide = (max(1.0, sigma * sigma_scale) for sigma in _POLISH_BAND)
    change = cv2.GaussianBlur(change, (0, 0), narrow) - cv2.GaussianBlur(change, (0, 0), wide)
    working_sigma = max(0.8, _POLISH_USM_SIGMA * sigma_scale)
    shrunk = [[v * scale for v in face[:4]] + [face[4]] for face in faces]
    adjusted = _polish_sharpen(lightness + change, working_sigma, amount, shrunk, 0)
    low, high = (float(v) for v in np.percentile(adjusted, [1, 99]))
    stretch, middle = 1.0, (low + high) / 2.0
    if 5.0 < high - low < _POLISH_LEVELS_SPAN:
        stretch = min(_POLISH_LEVELS_MAX, _POLISH_LEVELS_SPAN / (high - low))
        stretch = min(stretch, (middle - 0.5) / max(middle - low, 1e-6), (99.5 - middle) / max(high - middle, 1e-6))
        stretch = max(stretch, 1.0)
    return {"change": change, "scale": scale, "stretch": stretch, "middle": middle}


def run_polish(image, engine, variant):
    """``image`` polished at the variant's strengths, blended with the original by ``variant["blend"]``.

    ``variant``: ``{"blend", "clahe", "amount", "faces": [[x, y, w, h, detail], ...]}``. Returns ``(image, models)`` like every operation.
    """
    started = time.perf_counter()
    clahe_share, amount, blend = float(variant["clahe"]), float(variant["amount"]), float(variant["blend"])
    vibrance = float(variant.get("vibrance", _POLISH_VIBRANCE_MAX))          # the finishing polish (ADAPTIVE_ALWAYS_ENHANCE) asks for more
    faces = [list(face) for face in variant.get("faces", [])]
    plan = _polish_plan(image, clahe_share, amount, faces)
    height, width = image.shape[:2]
    change = cv2.resize(plan["change"], (width, height), interpolation=cv2.INTER_CUBIC) if plan["scale"] < 1.0 else plan["change"]
    long_edge = max(height, width)
    sigma = float(np.clip(_POLISH_USM_SIGMA / plan["scale"], 0.8, 8.0)) if plan["scale"] < 1.0 else max(0.8, _POLISH_USM_SIGMA * min(1.0, long_edge / _POLISH_WORK_EDGE))
    output = np.empty_like(image)
    for top in range(0, height, _POLISH_STRIP_ROWS):
        bottom = min(height, top + _POLISH_STRIP_ROWS)
        first, last = max(0, top - _POLISH_STRIP_PAD), min(height, bottom + _POLISH_STRIP_PAD)
        lab = _polish_lab(image[first:last])
        lightness = _polish_sharpen(lab[:, :, 0] + change[first:last], sigma, amount, faces, first)
        if plan["stretch"] > 1.0:
            lightness = plan["middle"] + (lightness - plan["middle"]) * plan["stretch"]
        lab[:, :, 0] = np.clip(lightness, 0.0, 100.0)
        chroma = np.hypot(lab[:, :, 1], lab[:, :, 2])
        gain = 1.0 + vibrance * np.clip(1.0 - chroma / _POLISH_VIBRANCE_CHROMA, 0.0, 1.0)
        lab[:, :, 1] *= gain
        lab[:, :, 2] *= gain
        polished = _polish_bgr(lab)[top - first:bottom - first]
        output[top:bottom] = _blend(image[top:bottom], polished, blend)
    return output, [_record(engine, {"provider": "opencv_polish_v1"}, time.perf_counter() - started,
                            clahe_share=clahe_share, amount=amount, stretch=round(plan["stretch"], 4))]

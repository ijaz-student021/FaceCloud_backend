"""Model-backed editor sliders: low light, noise reduction, deblur and face restoration.

Each slider is 0-100 and means "how much of the model's result to blend into the photo": 0 leaves the photo
alone, 100 is the model's full candidate. They run in a fixed order, before the tone controls:

    low light (Retinexformer or Zero-DCE) -> noise reduction (NAFNet-SIDD) -> deblur (NAFNet-GoPro)
    -> face restoration (CodeFormer) -> tone controls (brightness, contrast, ...)

A model's candidate is computed from the image it is given and cached under that image's hash, so moving one
slider recomputes only the stages after it and moving a later slider reuses the earlier candidates: the first
move is as slow as the model, the next ones are blends. A slider whose model is not installed or not switched
on falls back to a classical OpenCV equivalent where one exists (low light, noise reduction, deblur) and is
reported as unavailable where none does (face restoration); the response always says which engine ran.

The sliders are explicit user requests, so they are governed by each model's own setting
(``RETINEXFORMER_ENABLED``, ``NAFNET_DENOISE_ENABLED``, ...), not by the automatic pipeline's rendering policy.
"""
import hashlib
import threading
from collections import OrderedDict

import cv2
import numpy as np
from django.conf import settings

MODEL_CONTROL_NAMES = ("lowlight", "denoise", "deblur", "face_restore")
_CACHE_SIZE = 12
_CACHE = OrderedDict()
_LOCK = threading.Lock()


def _engines():
    """For each control, the engines in order of preference: ``(name, enabled?)``."""
    return {
        "lowlight": [("retinexformer", bool(settings.RETINEXFORMER_ENABLED)), ("zero_dce", bool(settings.ZERO_DCE_ENABLED)),
                     ("opencv_gamma", True)],
        "denoise": [("nafnet_sidd", bool(settings.NAFNET_DENOISE_ENABLED)), ("opencv_bilateral", True)],
        "deblur": [("nafnet_gopro", bool(settings.NAFNET_DEBLUR_ENABLED)), ("opencv_unsharp", True)],
        "face_restore": [("codeformer", bool(settings.CODEFORMER_ENABLED))],
    }


def engine_status():
    """What each slider will use right now, for ``GET /api/enhancements/editor/configuration/``."""
    status = {}
    for control, engines in _engines().items():
        chosen = next((name for name, enabled in engines if enabled), None)
        status[control] = {
            "engine": chosen,
            "available": chosen is not None,
            "uses_model": chosen is not None and not chosen.startswith("opencv"),
            "fallback_engine": next((name for name, enabled in engines[1:] if enabled and name != chosen), None),
        }
    return status


# -- engines -------------------------------------------------------------------

def _opencv_gamma(image):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness = lab[:, :, 0].astype(np.float32) / 255.0
    lab[:, :, 0] = np.clip(255.0 * np.power(lightness, 0.55), 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), {"provider": "opencv_gamma"}


def _opencv_bilateral(image):
    from .tone import _denoise

    lab = cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    # The filter's colour tolerances are on the 0-255 scale, as in the tone renderer.
    lightness, channel_a, channel_b = _denoise(lab[:, :, 0] * 2.55, lab[:, :, 1] + 128.0, lab[:, :, 2] + 128.0, 1.0, image.shape)
    lab[:, :, 0], lab[:, :, 1], lab[:, :, 2] = np.clip(lightness, 0, 255) / 2.55, channel_a - 128.0, channel_b - 128.0
    rendered = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return np.clip(np.rint(rendered * 255.0), 0, 255).astype(np.uint8), {"provider": "opencv_bilateral"}


def _opencv_unsharp(image):
    sigma = max(0.8, 1.2 * max(image.shape[:2]) / 1024.0)
    blurred = cv2.GaussianBlur(image, (0, 0), sigma)
    return cv2.addWeighted(image, 2.0, blurred, -1.0, 0), {"provider": "opencv_unsharp"}


def _retinexformer(image):
    from .lowlight import enhance_with_retinexformer

    return enhance_with_retinexformer(image)


def _zero_dce(image):
    from .zero_dce import enhance_with_zero_dce

    return enhance_with_zero_dce(image)


def _nafnet_sidd(image):
    from .hd_restoration import denoise_with_nafnet

    return denoise_with_nafnet(image)


def _nafnet_gopro(image):
    from .hd_restoration import deblur_with_nafnet

    return deblur_with_nafnet(image)


def _codeformer(image):
    from .codeformer import restore_faces_codeformer

    return restore_faces_codeformer(image)


_RUNNERS = {
    "retinexformer": _retinexformer, "zero_dce": _zero_dce, "opencv_gamma": _opencv_gamma,
    "nafnet_sidd": _nafnet_sidd, "opencv_bilateral": _opencv_bilateral,
    "nafnet_gopro": _nafnet_gopro, "opencv_unsharp": _opencv_unsharp,
    "codeformer": _codeformer,
}


def _fingerprint(image):
    return hashlib.blake2b(np.ascontiguousarray(image).tobytes(), digest_size=16).hexdigest() + f"{image.shape}"


def _candidate(control, engine, image):
    """The engine's full-strength candidate for this exact image, from the cache when it has one."""
    key = (control, engine, _fingerprint(image))
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            candidate, metadata = _CACHE[key]
            return candidate, {**metadata, "cached": True}
    candidate, metadata = _RUNNERS[engine](image)
    if candidate.shape != image.shape:
        raise ValueError("A slider model must not change the image size.")
    with _LOCK:
        _CACHE[key] = (candidate, metadata)
        budget = int(settings.EDITOR_MODEL_CACHE_MB) * 1024 * 1024
        # Bounded by count and by bytes: a full-resolution candidate is tens of megabytes.
        while len(_CACHE) > _CACHE_SIZE or (len(_CACHE) > 1 and sum(item[0].nbytes for item in _CACHE.values()) > budget):
            _CACHE.popitem(last=False)
    return candidate, {**metadata, "cached": False}


def clear_cache():
    with _LOCK:
        _CACHE.clear()


def apply_model_controls(source, controls, strength=1.0):
    """Apply the model-backed sliders in order. Returns ``(image, report, fallback_reasons)``.

    ``controls`` holds 0-100 values by name; ``strength`` (0-1) scales every one, which is how the safety
    guards reduce a render. ``report`` has one entry per slider that was moved.
    """
    from .codeformer import CodeFormerUnavailable
    from .hd_restoration import HDRestorationUnavailable
    from .lowlight import LowLightUnavailable
    from .zero_dce import ZeroDCEUnavailable

    failures = (CodeFormerUnavailable, HDRestorationUnavailable, LowLightUnavailable, ZeroDCEUnavailable, ValueError)
    engines = _engines()
    image = source
    report, reasons = [], []
    for control in MODEL_CONTROL_NAMES:
        value = float(controls.get(control, 0.0)) * strength
        if value <= 0:
            continue
        amount = min(value, 100.0) / 100.0
        entry = {"control": control, "requested": round(value, 2), "blend": round(amount, 3), "engine": None, "applied": False}
        used = False
        for engine, enabled in engines[control]:
            if not enabled:
                continue
            try:
                candidate, metadata = _candidate(control, engine, image)
            except failures as error:
                entry.setdefault("skipped_engines", []).append({"engine": engine, "reason": str(error)})
                reasons.append(f"{control}_{engine}_unavailable")
                continue
            image = cv2.addWeighted(candidate, amount, image, 1.0 - amount, 0)
            entry.update({"engine": engine, "applied": True, "cached": metadata.get("cached"),
                          "device": metadata.get("device"), "provider": metadata.get("provider")})
            used = True
            break
        if not used:
            entry["reason"] = "No engine for this control is available."
            reasons.append(f"{control}_not_applied")
        report.append(entry)
    return image, report, reasons

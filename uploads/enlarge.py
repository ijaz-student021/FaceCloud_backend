"""Enlarging a very small photograph while it is standardized (``SMALL_IMAGE_ENLARGE_ENABLED``, off by default).

A photograph whose shorter side is under ``SMALL_IMAGE_ENLARGE_BELOW_EDGE`` pixels is refused by the quality check for its size
(``resolution_too_low``) and so never reaches face validation's size rules, grouping or enhancement. With the setting on it is enlarged
2 or 4 times here, in preprocessing, so every later stage sees a usable image. The uploaded original is never changed; the standardized copy
is the enlarged one, and ``ImagePreprocessingResult.enlargement`` says how it was made.

What it can and cannot do (measured, docs/evaluation/2026-11-02-small-image-enlargement.md): no method adds information that is not in the
pixels. OpenCV (Lanczos) is deterministic and invents nothing but is very soft (a no-face photograph then reads as blurred, so the quality
check may still send it to review for blur). Real-ESRGAN, blended with a cubic enlargement like the enhancement engine does, is a little closer to the true
larger photograph on average and sharper, and may invent plausible detail (once, a second, doubtful face). Enlargement is never reported as
restoration: the result records the engine, the scale and the original size, and the quality decision carries ``enlarged_from``.

Enlarging never fails an upload: any problem leaves the photograph as it was, and the reason is recorded.
"""
import logging
import time

import cv2
import numpy as np
from django.conf import settings
from PIL import Image

logger = logging.getLogger(__name__)

OPENCV_ENGINE = "opencv_lanczos"
REALESRGAN_ENGINE = "realesrgan_x4plus"


def enlargement_scale(width, height):
    """The factor (2 or 4) by which a ``width`` x ``height`` photograph is to be enlarged, or 0 for "leave it alone".

    It is enlarged only when the setting is on and its shorter side is under ``SMALL_IMAGE_ENLARGE_BELOW_EDGE``; the factor is the smallest of 2
    and 4 that brings the shorter side to ``SMALL_IMAGE_ENLARGE_TARGET_EDGE`` (4 when even that falls short), and is lowered if the result would
    exceed the preprocessing size limit.
    """
    if not settings.SMALL_IMAGE_ENLARGE_ENABLED:
        return 0
    shorter = min(width, height)
    if shorter <= 0 or shorter >= settings.SMALL_IMAGE_ENLARGE_BELOW_EDGE:
        return 0
    scale = 2 if shorter * 2 >= settings.SMALL_IMAGE_ENLARGE_TARGET_EDGE else 4
    while scale >= 2 and (width * scale > settings.PREPROCESS_MAX_WIDTH or height * scale > settings.PREPROCESS_MAX_HEIGHT):
        scale //= 2
    return scale if scale >= 2 else 0


def _lanczos(bgr, scale):
    return cv2.resize(bgr, (bgr.shape[1] * scale, bgr.shape[0] * scale), interpolation=cv2.INTER_LANCZOS4)


def _model(bgr, scale):
    """Real-ESRGAN, blended with a cubic enlargement as the enhancement engine does; refused if it no longer looks like the input."""
    from enhancement.hd_restoration import upscale_with_realesrgan

    raw, _metadata = upscale_with_realesrgan(bgr, output_scale=scale)
    if raw.shape[:2] != (bgr.shape[0] * scale, bgr.shape[1] * scale) or raw.dtype != np.uint8:
        raise ValueError("The model returned an image of an unexpected size.")
    cubic = cv2.resize(bgr, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_CUBIC)
    blend = min(max(float(settings.REALESRGAN_OUTPUT_BLEND), 0.0), 1.0)
    out = raw if blend >= 0.999 else cv2.addWeighted(raw, blend, cubic, 1.0 - blend, 0)
    shrunk = cv2.resize(out, (bgr.shape[1], bgr.shape[0]), interpolation=cv2.INTER_AREA)
    consistency = float(cv2.PSNR(bgr, shrunk))
    if consistency < settings.SMALL_IMAGE_ENLARGE_MIN_CONSISTENCY_DB:
        raise ValueError(f"The model's result does not match the input ({consistency:.1f} dB).")
    return out


def _enlarge_bgr(bgr, scale):
    """``(enlarged BGR image, engine name, why the model was not used)``."""
    engine = settings.SMALL_IMAGE_ENLARGE_ENGINE
    reason = ""
    if engine in ("auto", "realesrgan"):
        if not settings.HD_RESTORATION_ENABLED:
            if engine == "realesrgan":
                raise ValueError("SMALL_IMAGE_ENLARGE_ENGINE is realesrgan but HD_RESTORATION_ENABLED is off.")
            reason = "HD_RESTORATION_ENABLED is off, so OpenCV was used."
        else:
            try:
                return _model(bgr, scale), REALESRGAN_ENGINE, ""
            except Exception as error:   # a missing model, no PyTorch, a refused result: all mean "no model"
                reason = f"{type(error).__name__}: {str(error)[:160]}"
                logger.warning("Small-image enlargement: the model was not used (%s).", reason)
                if engine == "realesrgan":
                    raise
    return _lanczos(bgr, scale), OPENCV_ENGINE, reason


def enlarge_small_image(working):
    """``(image, info)``: ``working`` (a PIL image in RGB or RGBA) enlarged when it is small, else returned as it is.

    ``info`` is ``{}`` when nothing was asked of it, else ``{"applied": bool, "from": [w, h], "to": [w, h], "scale", "engine", "seconds",
    "fallback_reason"}``; when it did not apply, ``"reason"`` says why. Never raises.
    """
    width, height = working.size
    scale = enlargement_scale(width, height)
    if not scale:
        return working, {}
    started = time.perf_counter()
    info = {"applied": False, "from": [width, height], "scale": scale}
    try:
        rgb = np.asarray(working.convert("RGB"))
        bgr, alpha = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), None
        if working.mode == "RGBA":
            alpha = np.asarray(working.getchannel("A"))
        enlarged, engine, fallback = _enlarge_bgr(bgr, scale)
        out_rgb = cv2.cvtColor(enlarged, cv2.COLOR_BGR2RGB)
        result = Image.fromarray(out_rgb)
        if alpha is not None:
            big_alpha = cv2.resize(alpha, (out_rgb.shape[1], out_rgb.shape[0]), interpolation=cv2.INTER_LANCZOS4)
            result = result.convert("RGBA")
            result.putalpha(Image.fromarray(big_alpha))
    except Exception as error:   # enlarging is a courtesy to a small photograph: it must never fail the upload
        logger.exception("Small-image enlargement failed; the photograph is kept as it is.")
        info["reason"] = f"{type(error).__name__}: {str(error)[:160]}"
        return working, info
    info.update(applied=True, to=[result.size[0], result.size[1]], engine=engine, fallback_reason=fallback,
                seconds=round(time.perf_counter() - started, 2))
    return result, info

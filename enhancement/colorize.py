"""DDColor colourization of a black-and-white photograph (Kang et al., ICCV 2023; Apache-2.0).

The network sees a 512 x 512 copy and predicts only the two chroma channels (a, b). They are enlarged and combined with the photograph's own full-resolution
lightness, so no detail is added, lost or invented: the result has exactly the luminance of the input.

A prediction is a guess (a black-and-white photograph does not contain colour), and a raw guess is more saturated than real photographs: on the owner's portrait the
small model turned teeth orange and on a dark portrait it painted the wall blue. The prediction is therefore damped before it is used (see the DDCOLOR_* settings and
docs/evaluation/2026-11-03-colourization.md): everywhere by ``DDCOLOR_CHROMA_SCALE``, less in the darkest pixels (which carry little real chroma), more in the
brightest (teeth, eye whites, clouds), and capped at ``DDCOLOR_MAX_CHROMA``. The caller (the adaptive engine) decides whether the photograph is black and white and
validates the result; this module only produces it, from a checksum-verified checkpoint, and says so when it cannot.
"""
import time
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

from . import runtime
from .hd_restoration import HDRestorationUnavailable, _verified_path

_MODEL_LOCK = runtime.INFERENCE_LOCK
_MODEL_CACHE = runtime.register_model_cache({})
_ENCODERS = {"tiny": "convnext-t", "large": "convnext-l"}
_STRIP_ROWS = 1024      # the full-resolution colour work is done in strips so memory does not grow with the photograph


class DDColorUnavailable(RuntimeError):
    """The colourization model cannot run safely (missing, wrong checksum, incompatible, out of memory)."""


def _torch():
    try:
        import torch
    except ImportError as error:
        raise DDColorUnavailable("The isolated PyTorch runtime is unavailable.") from error
    return torch


def _load_model():
    try:
        path = _verified_path(settings.DDCOLOR_MODEL_PATH, settings.DDCOLOR_MODEL_SHA256, "DDColor")
    except HDRestorationUnavailable as error:
        raise DDColorUnavailable(str(error)) from error
    torch = _torch()
    device = runtime.resolve_device(torch, settings.DDCOLOR_DEVICE, "DDColor", DDColorUnavailable)
    key = (settings.DDCOLOR_MODEL_VARIANT, str(path), str(device), int(settings.DDCOLOR_INPUT_SIZE))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device, path
    runtime.make_room(_MODEL_CACHE)       # on a small machine only one large model stays loaded
    from .archs.ddcolor.model import DDColor

    size = int(settings.DDCOLOR_INPUT_SIZE)
    model = DDColor(encoder_name=_ENCODERS[settings.DDCOLOR_MODEL_VARIANT], decoder_name="MultiScaleColorDecoder", input_size=[size, size],
                    num_output_channels=2, last_norm="Spectral", do_normalize=False, num_queries=100, num_scales=3, dec_layers=9)
    try:
        state = torch.load(path, map_location="cpu", weights_only=True)
        state = state.get("params", state) if isinstance(state, dict) else state
        outcome = model.load_state_dict(state, strict=False)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise DDColorUnavailable("The DDColor checkpoint is incompatible.") from error
    if outcome.missing_keys or outcome.unexpected_keys:   # strict=False is how the authors load it; a silent partial load would be a random network
        raise DDColorUnavailable(f"The DDColor checkpoint does not match the network ({len(outcome.missing_keys)} missing, {len(outcome.unexpected_keys)} unexpected tensors).")
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device, path


def _smoothstep(values, low, high):
    t = np.clip((values - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def natural_chroma_factor(lightness, scale=None, dark=None, highlight=None):
    """The per-pixel factor applied to the predicted chroma (see the module docstring); ``lightness`` is CIELAB L in 0-100."""
    scale = settings.DDCOLOR_CHROMA_SCALE if scale is None else scale
    dark = settings.DDCOLOR_DARK_DAMPING if dark is None else dark
    highlight = settings.DDCOLOR_HIGHLIGHT_DAMPING if highlight is None else highlight
    factor = np.full(lightness.shape, scale, np.float32)
    if dark < 1.0:
        factor *= dark + (1.0 - dark) * _smoothstep(lightness, 8.0, 35.0)
    if highlight > 0.0:
        factor *= 1.0 - highlight * _smoothstep(lightness, 80.0, 96.0)
    return factor


def _predict_ab(model, device, image):
    """The model's a/b prediction for ``image`` at its own square input size: ``(2, size, size)`` float32 in CIELAB units."""
    torch = _torch()
    size = int(settings.DDCOLOR_INPUT_SIZE)
    small = cv2.resize(image.astype(np.float32) / 255.0, (size, size), interpolation=cv2.INTER_AREA)
    lightness = cv2.cvtColor(small, cv2.COLOR_BGR2Lab)[:, :, :1]
    grey_rgb = cv2.cvtColor(np.concatenate((lightness, np.zeros_like(lightness), np.zeros_like(lightness)), axis=-1), cv2.COLOR_LAB2RGB)
    tensor = torch.from_numpy(np.ascontiguousarray(grey_rgb.transpose(2, 0, 1))).float().unsqueeze(0).to(device)
    with torch.inference_mode():
        return model(tensor).float().cpu()[0].numpy()


def colorize_with_ddcolor(image):
    """``(colourized BGR uint8 image, metadata)`` for a black-and-white BGR photograph; raises ``DDColorUnavailable`` when it cannot run."""
    if image is None or image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise DDColorUnavailable("Colourization needs a BGR uint8 image.")
    started = time.perf_counter()
    runtime.check_deadline()
    with _MODEL_LOCK:
        model, device, path = _load_model()
        try:
            ab_small = _predict_ab(model, device, image)
        except RuntimeError as error:
            if runtime.is_out_of_memory(error):
                runtime.release_memory(device)
                raise DDColorUnavailable("The device ran out of memory colourizing this photograph.") from error
            raise DDColorUnavailable("The DDColor network failed on this photograph.") from error
    height, width = image.shape[:2]
    ab = np.dstack([cv2.resize(channel, (width, height), interpolation=cv2.INTER_CUBIC) for channel in ab_small]).astype(np.float32)
    output = np.empty_like(image)
    maximum = float(settings.DDCOLOR_MAX_CHROMA)
    for top in range(0, height, _STRIP_ROWS):
        bottom = min(height, top + _STRIP_ROWS)
        lab = cv2.cvtColor(image[top:bottom].astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
        chroma_ab = ab[top:bottom] * natural_chroma_factor(lab[:, :, 0])[..., None]
        magnitude = np.hypot(chroma_ab[..., 0], chroma_ab[..., 1])
        chroma_ab *= np.minimum(1.0, maximum / np.maximum(magnitude, 1e-6))[..., None]
        lab[:, :, 1:] = chroma_ab
        output[top:bottom] = np.clip(np.rint(cv2.cvtColor(lab, cv2.COLOR_Lab2BGR) * 255.0), 0, 255).astype(np.uint8)
    metadata = {
        "provider": f"ddcolor_{settings.DDCOLOR_MODEL_VARIANT}", "device": str(device), "checkpoint_sha256_prefix": str(settings.DDCOLOR_MODEL_SHA256)[:12],
        "checkpoint": Path(path).name, "input_size": int(settings.DDCOLOR_INPUT_SIZE), "chroma_scale": settings.DDCOLOR_CHROMA_SCALE,
        "dark_damping": settings.DDCOLOR_DARK_DAMPING, "highlight_damping": settings.DDCOLOR_HIGHLIGHT_DAMPING, "max_chroma": maximum,
        "seconds": round(time.perf_counter() - started, 2),
    }
    return output, metadata

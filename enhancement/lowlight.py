"""Retinexformer (ICCV 2023) low-light enhancement: a checksum-pinned local PyTorch adapter.

The network is the authors' (MIT licence, vendored in ``archs/retinexformer.py``) and runs the authors'
released checkpoints strictly. It follows the same contract as the other model adapters: it never raises
anything but ``LowLightUnavailable``, runs one heavy inference at a time per process, retries smaller tiles
when memory runs out, and returns ``(image, metadata)`` so the pipeline's guards judge the candidate.
"""
import hashlib
from pathlib import Path

from django.conf import settings

from . import runtime


class LowLightUnavailable(RuntimeError):
    """Raised when Retinexformer cannot run safely; the pipeline falls back to Zero-DCE or OpenCV."""


_MODEL_LOCK = runtime.INFERENCE_LOCK
_MODEL_CACHE = {}


def _torch():
    try:
        import torch
    except ImportError as error:
        raise LowLightUnavailable("The PyTorch runtime is unavailable.") from error
    return torch


def _verified_path(path_value, expected_hash):
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise LowLightUnavailable("The Retinexformer checkpoint is unavailable.")
    expected = str(expected_hash).strip().lower()
    if len(expected) != 64:
        raise LowLightUnavailable("The Retinexformer checksum is not configured.")
    hasher = hashlib.sha256()
    with path.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != expected:
        raise LowLightUnavailable("The Retinexformer checkpoint failed integrity verification.")
    return path


def _build(torch):
    from torch.nn import functional

    try:
        from .archs.retinexformer import RetinexFormer
    except ImportError as error:  # einops is the one extra dependency
        raise LowLightUnavailable("The Retinexformer dependencies (einops) are not installed.") from error

    class Padded(torch.nn.Module):
        """The network halves the image twice, so height and width must be multiples of 4."""

        def __init__(self, body):
            super().__init__()
            self.body = body

        def forward(self, value):
            height, width = value.shape[-2:]
            pad_h, pad_w = (-height) % 4, (-width) % 4
            if pad_h or pad_w:
                mode = "reflect" if pad_h < height and pad_w < width else "replicate"
                value = functional.pad(value, (0, pad_w, 0, pad_h), mode=mode)
            return self.body(value)[:, :, :height, :width]

    return Padded(RetinexFormer(in_channels=3, out_channels=3, n_feat=40, stage=1, num_blocks=[1, 2, 2]))


def _load_model():
    torch = _torch()
    path = _verified_path(settings.RETINEXFORMER_MODEL_PATH, settings.RETINEXFORMER_MODEL_SHA256)
    device = runtime.resolve_device(torch, settings.RETINEXFORMER_DEVICE, "Retinexformer", LowLightUnavailable)
    key = (str(path), str(device))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    model = _build(torch)
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        model.body.load_state_dict(checkpoint.get("params", checkpoint), strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise LowLightUnavailable("The Retinexformer checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


def enhance_with_retinexformer(image):
    """Brighten a dark photo. Returns ``(bgr_uint8, metadata)``."""
    if image is None or image.size == 0:
        raise LowLightUnavailable("The Retinexformer input image is invalid.")
    torch = _torch()
    with _MODEL_LOCK:
        model, device = _load_model()
        output, inference = runtime.run_with_memory_fallback(
            lambda tile, overlap: runtime.tiled_inference(torch, image, model, device, 1, tile, overlap),
            device=device,
            tile=settings.RETINEXFORMER_TILE_SIZE, overlap=settings.RETINEXFORMER_TILE_OVERLAP,
            label="Retinexformer", error_class=LowLightUnavailable,
        )
    return output, {
        "provider": "retinexformer_ntire",
        "checkpoint_sha256_prefix": settings.RETINEXFORMER_MODEL_SHA256[:12],
        **inference,
    }

"""Retinexformer (ICCV 2023) low-light enhancement: a checksum-pinned local PyTorch adapter.

The network is the authors' (MIT licence, vendored in ``archs/retinexformer.py``) and runs the authors'
released checkpoints strictly. It follows the same contract as the other model adapters: it never raises
anything but ``LowLightUnavailable``, runs one heavy inference at a time per process, retries smaller tiles
when memory runs out, and returns ``(image, metadata)`` so the pipeline's guards judge the candidate.
"""
import hashlib
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

from . import runtime


class LowLightUnavailable(RuntimeError):
    """Raised when Retinexformer cannot run safely; the pipeline falls back to Zero-DCE or OpenCV."""


_MODEL_LOCK = runtime.INFERENCE_LOCK
_MODEL_CACHE = runtime.register_model_cache({})


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
    runtime.make_room(_MODEL_CACHE)
    model = _build(torch)
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        model.body.load_state_dict(checkpoint.get("params", checkpoint), strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise LowLightUnavailable("The Retinexformer checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


GAIN_LIMIT = 20.0          # the largest brightening applied to any pixel
GAIN_BLUR_SIGMA = 3.0      # the gain is smoothed (in pixels of the small copy) so it follows the lighting, not the noise
GAIN_BAND_ROWS = 512       # the gain is applied in bands of this many rows, so a 24 MP photograph needs no full-size float copy


def transfer_gain(full, small_in, small_out):
    """Apply to ``full`` the brightening the network made to its small copy (``small_in`` became ``small_out``): ``(bgr_uint8)`` the size of ``full``.

    The gain is the per-channel ratio of the two small images, smoothed and enlarged, and multiplied into the full-size photograph, so the full-size
    detail is kept and the lighting is the network's. Measured against running the network tile by tile at the full size (``docs/evaluation/2026-11-03-low-light-engines.md``).
    """
    gain = np.clip((small_out.astype(np.float32) + 1.0) / (small_in.astype(np.float32) + 1.0), 1.0, GAIN_LIMIT)
    gain = cv2.GaussianBlur(gain, (0, 0), GAIN_BLUR_SIGMA)
    height, width = full.shape[:2]
    scale_x, scale_y = gain.shape[1] / width, gain.shape[0] / height
    result = np.empty_like(full)
    for top in range(0, height, GAIN_BAND_ROWS):
        bottom = min(height, top + GAIN_BAND_ROWS)
        # destination pixel (x, y) reads the gain at ((x + .5) * scale_x - .5, (top + y + .5) * scale_y - .5): the same grid cv2.resize uses
        matrix = np.array([[scale_x, 0.0, 0.5 * scale_x - 0.5], [0.0, scale_y, (top + 0.5) * scale_y - 0.5]], dtype=np.float32)
        band = cv2.warpAffine(gain, matrix, (width, bottom - top), flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
        result[top:bottom] = np.clip(np.rint(full[top:bottom].astype(np.float32) * np.maximum(band, 1.0)), 0, 255).astype(np.uint8)
    return result


def enhance_with_retinexformer(image):
    """Brighten a dark photo. Returns ``(bgr_uint8, metadata)``.

    A photograph whose long edge is over ``RETINEXFORMER_WORK_EDGE`` is NOT run through the network tile by tile: each tile estimates the lighting by itself, so
    the tiles differ and the seams show (measured on three dark portraits), and a large tile does not fit the memory of an 8 GB machine. The network runs once on a
    copy reduced to that edge and its gain is applied to the full-size photograph (``transfer_gain``). ``0`` switches this off (the old tiled run at full size).
    """
    if image is None or image.size == 0:
        raise LowLightUnavailable("The Retinexformer input image is invalid.")
    torch = _torch()
    work_edge = int(settings.RETINEXFORMER_WORK_EDGE)
    source = image
    if work_edge > 0 and max(image.shape[:2]) > work_edge:
        ratio = work_edge / max(image.shape[:2])
        source = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    with _MODEL_LOCK:
        model, device = _load_model()
        tile = int(settings.RETINEXFORMER_TILE_SIZE)
        if getattr(device, "type", str(device)) == "cpu" and max(source.shape[:2]) > tile:      # one tile (the one-pass copy) is never cut smaller
            # Measured on CPU (docs/deployment/hostinger-readiness-2026-10.md): 1024-px tiles peak at 2.7 GB and took 56-70 s at 1.2 MP,
            # 512-px tiles 1.7 GB and 24 s, with the same quality (PSNR to the clean photograph within 0.06 dB; 256 loses up to 0.9 dB).
            tile = min(tile, int(settings.RETINEXFORMER_CPU_TILE_SIZE))
        output, inference = runtime.run_with_memory_fallback(
            lambda tile, overlap: runtime.tiled_inference(torch, source, model, device, 1, tile, overlap),
            device=device,
            tile=tile, overlap=settings.RETINEXFORMER_TILE_OVERLAP,
            label="Retinexformer", error_class=LowLightUnavailable,
        )
    if source is not image:
        output = transfer_gain(image, source, output)
        inference = {**inference, "one_pass_work_size": [int(source.shape[1]), int(source.shape[0])]}
    return output, {
        "provider": "retinexformer_ntire",
        "checkpoint_sha256_prefix": settings.RETINEXFORMER_MODEL_SHA256[:12],
        **inference,
    }

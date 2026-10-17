"""Inference runtime shared by the PyTorch model adapters.

Each adapter used to carry its own copy of device selection and tiling, with no
handling of a device running out of memory. This module is the one place that
decides which device a model runs on, bounds how much memory an inference may
hold, and turns an out-of-memory error into a bounded retry or a safe fallback.

Nothing here imports PyTorch: adapters pass in the ``torch`` module they loaded,
so a deployment without it still starts and falls back cleanly.
"""
import gc
import logging
import sys
import threading

import cv2
import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

# A model's raw output is expected in [0, 1]. A tile whose output leaves this
# band by more than the margin has diverged (NAFNet does on some near-black
# tiles: about +-390 on a dark hair tile, on CPU and MPS alike); it is replaced
# by its own input, resized, rather than clamped into colour noise.
VALID_OUTPUT_MARGIN = 0.5
_TILE_STATS = threading.local()


def last_tile_stats():
    """``{"tiles": n, "tiles_rejected": m}`` for the most recent tiled inference."""
    return dict(getattr(_TILE_STATS, "value", {"tiles": 0, "tiles_rejected": 0}))


# One heavy inference at a time in a worker process, whichever model runs it.
# Two models on one GPU at once is the usual way to run out of memory.
INFERENCE_LOCK = threading.RLock()


class InferenceMemoryError(RuntimeError):
    """Inference ran out of memory even at the smallest allowed tile."""


def is_out_of_memory(error):
    """True for CUDA, MPS and host out-of-memory errors, across PyTorch versions."""
    if isinstance(error, MemoryError):
        return True
    if type(error).__name__ == "OutOfMemoryError":  # torch.OutOfMemoryError / torch.cuda.OutOfMemoryError
        return True
    return isinstance(error, RuntimeError) and "out of memory" in str(error).lower()


def release_memory(device):
    """Give cached device memory back before a retry."""
    gc.collect()
    torch = sys.modules.get("torch")  # never import the runtime just to free memory
    if torch is None:
        return
    device_type = getattr(device, "type", str(device))
    try:
        if device_type == "cuda":
            torch.cuda.empty_cache()
        elif device_type == "mps" and hasattr(torch, "mps"):
            torch.mps.empty_cache()
    except RuntimeError:  # an allocator that is itself in trouble must not mask the fallback
        logger.warning("Could not release cached %s memory.", device_type)


def _mps_available(torch):
    backend = getattr(torch.backends, "mps", None)
    return bool(backend is not None and backend.is_available())


def auto_device(torch):
    """The best device the installed runtime really supports: CUDA, then MPS, then CPU."""
    if torch.cuda.is_available():
        return "cuda"
    if _mps_available(torch):
        return "mps"
    return "cpu"


def resolve_device(torch, configured, label, error_class):
    """Turn a ``*_DEVICE`` setting into a usable ``torch.device`` or refuse.

    ``auto`` picks the best available device. An explicitly configured GPU that is
    not there is an error, never a silent move to the CPU: an operator who asked
    for ``cuda`` should find out it is missing, not wonder why a job takes an hour.
    """
    configured = str(configured).strip().lower()
    if configured == "auto":
        configured = auto_device(torch)
    if configured.startswith("cuda") and not torch.cuda.is_available():
        raise error_class(f"The configured {label} CUDA device is unavailable.")
    if configured == "mps" and not _mps_available(torch):
        raise error_class(f"The configured {label} MPS device is unavailable.")
    try:
        return torch.device(configured)
    except (RuntimeError, ValueError) as error:
        raise error_class(f"The configured {label} device is invalid.") from error


def _positions(length, tile, step):
    last = max(length - tile, 0)
    positions = list(range(0, last + 1, step))
    if not positions or positions[-1] != last:
        positions.append(last)
    return positions


def tensor_from_bgr(image, torch, device):
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).unsqueeze(0).to(device)


def bgr_from_tensor(tensor):
    rgb = tensor.detach().float().clamp_(0, 1).squeeze(0).cpu().numpy().transpose(1, 2, 0)
    return cv2.cvtColor(np.rint(rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def tiled_inference(torch, image, model, device, scale, tile, overlap, output_scale=None):
    """Run ``model`` over overlapping tiles and average where they overlap.

    ``scale`` is the model's native factor. When ``output_scale`` is smaller (a 4x
    model asked for a 2x result), each tile is reduced before it is accumulated,
    so the buffers are the size of the *output*, not of the native result. The
    reduction is an exact box average on tile origins that are multiples of the
    ratio, so it matches reducing the whole native image to within rounding.

    Buffers are integer sums (uint16) with a uint8 overlap count: 7 bytes per
    output pixel instead of 16 for float32.
    """
    output_scale = scale if output_scale is None else int(output_scale)
    if output_scale < 1 or output_scale > scale or scale % output_scale:
        raise ValueError("output_scale must divide the model's native scale.")
    height, width = image.shape[:2]
    tile = max(1, int(tile))
    # Overlap is capped at half a tile, so no pixel is covered more than a few
    # times and the integer sums below cannot overflow.
    step = max(1, tile - max(0, min(int(overlap), tile // 2)))
    total = np.zeros((height * output_scale, width * output_scale, 3), dtype=np.uint16)
    count = np.zeros((height * output_scale, width * output_scale, 1), dtype=np.uint8)
    stats = {"tiles": 0, "tiles_rejected": 0}
    _TILE_STATS.value = stats
    with torch.inference_mode():
        for y in _positions(height, tile, step):
            for x in _positions(width, tile, step):
                patch = image[y:min(y + tile, height), x:min(x + tile, width)]
                raw = model(tensor_from_bgr(patch, torch, device))
                patch_height, patch_width = patch.shape[:2]
                expected = (patch_height * output_scale, patch_width * output_scale)
                stats["tiles"] += 1
                low, high = float(raw.min()), float(raw.max())
                if not (np.isfinite(low) and np.isfinite(high)) or low < -VALID_OUTPUT_MARGIN or high > 1 + VALID_OUTPUT_MARGIN:
                    stats["tiles_rejected"] += 1
                    prediction = cv2.resize(patch, (expected[1], expected[0]), interpolation=cv2.INTER_CUBIC)
                else:
                    prediction = bgr_from_tensor(raw)
                if prediction.shape[:2] != expected:
                    native = (patch_height * scale, patch_width * scale)
                    if prediction.shape[:2] != native:
                        raise ValueError("The model returned an unexpected tile size.")
                    prediction = cv2.resize(
                        prediction, (expected[1], expected[0]), interpolation=cv2.INTER_AREA,
                    )
                top, left = y * output_scale, x * output_scale
                total[top:top + expected[0], left:left + expected[1]] += prediction
                count[top:top + expected[0], left:left + expected[1]] += 1
    count = np.maximum(count, 1).astype(np.uint16)
    return ((total + count // 2) // count).astype(np.uint8)


def run_with_memory_fallback(run, *, device, tile, overlap, label, error_class):
    """Call ``run(tile, overlap)``; on out-of-memory, retry with smaller tiles.

    Tile area is quartered on each retry, at most ``ENHANCEMENT_OOM_MAX_RETRIES``
    times and never below ``ENHANCEMENT_MIN_TILE_SIZE``. If that is not enough the
    adapter's "unavailable" error is raised, so the pipeline keeps its last safe
    image. Any other inference error is raised the same way: a model failure
    never escapes as a raw runtime error.

    Returns ``(output, metadata)`` recording the device and tile actually used.
    """
    max_retries = max(0, int(settings.ENHANCEMENT_OOM_MAX_RETRIES))
    minimum_tile = max(16, int(settings.ENHANCEMENT_MIN_TILE_SIZE))
    tile = max(minimum_tile, int(tile))
    overlap = max(0, min(int(overlap), tile // 4))
    retries = 0
    while True:
        _TILE_STATS.value = {"tiles": 0, "tiles_rejected": 0}  # never report a previous run's counts
        try:
            output = run(tile, overlap)
        except Exception as error:
            if isinstance(error, error_class):
                raise
            if not is_out_of_memory(error):
                if isinstance(error, (RuntimeError, ValueError, TypeError)):
                    logger.exception("%s inference failed.", label)
                    raise error_class(f"{label} inference failed safely.") from error
                raise
            release_memory(device)
            if retries >= max_retries or tile <= minimum_tile:
                logger.error(
                    "%s ran out of memory on %s at tile %s after %s retries.",
                    label, device, tile, retries,
                )
                raise error_class(
                    f"{label} ran out of memory on {device}; the image was left as it was."
                ) from error
            retries += 1
            tile = max(minimum_tile, tile // 2)
            overlap = min(overlap, tile // 4)
            logger.warning("%s ran out of memory on %s; retrying with tile %s.", label, device, tile)
            continue
        return output, {
            "device": str(device),
            "tile_size": tile,
            "tile_overlap": overlap,
            "memory_retries": retries,
            **last_tile_stats(),
        }

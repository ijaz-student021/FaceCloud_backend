"""Private, checksum-pinned GFPGAN inference adapter.

The optional dependency is imported lazily so the main Django process remains
usable when the isolated restoration environment is not installed.  The
checkpoint is never downloaded automatically and must remain outside Git.
"""

import hashlib
import threading
from pathlib import Path

import numpy as np
from django.conf import settings


class GFPGANUnavailable(RuntimeError):
    """Raised when private face restoration cannot run safely."""


_MODEL_LOCK = threading.RLock()
_INFERENCE_LOCK = threading.Lock()
_MODEL_CACHE = {}


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _import_runtime():
    try:
        import torch
        from gfpgan import GFPGANer
    except (ImportError, OSError) as error:
        raise GFPGANUnavailable(
            "The isolated GFPGAN runtime is unavailable."
        ) from error
    return torch, GFPGANer


def _resolve_device(torch):
    configured = str(settings.GFPGAN_DEVICE).strip().lower()
    if configured == "auto":
        configured = "cuda" if torch.cuda.is_available() else "cpu"
    if configured == "cuda" and not torch.cuda.is_available():
        raise GFPGANUnavailable("The configured GFPGAN CUDA device is unavailable.")
    try:
        return torch.device(configured)
    except (RuntimeError, ValueError) as error:
        raise GFPGANUnavailable("The configured GFPGAN device is invalid.") from error


def _load_restorer():
    model_path = Path(settings.GFPGAN_MODEL_PATH).expanduser().resolve()
    expected_sha256 = str(settings.GFPGAN_MODEL_SHA256).strip().lower()
    if not model_path.is_file():
        raise GFPGANUnavailable("The private GFPGAN checkpoint is unavailable.")
    if not expected_sha256 or len(expected_sha256) != 64:
        raise GFPGANUnavailable("The GFPGAN checkpoint checksum is not configured.")

    stat = model_path.stat()
    cache_key = (
        str(model_path),
        stat.st_size,
        stat.st_mtime_ns,
        expected_sha256,
        settings.GFPGAN_ARCH,
        settings.GFPGAN_CHANNEL_MULTIPLIER,
        settings.GFPGAN_DEVICE,
    )
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached
        if _sha256(model_path) != expected_sha256:
            raise GFPGANUnavailable(
                "The private GFPGAN checkpoint failed integrity verification."
            )

        torch, restorer_class = _import_runtime()
        try:
            restorer = restorer_class(
                model_path=str(model_path),
                upscale=1,
                arch=settings.GFPGAN_ARCH,
                channel_multiplier=settings.GFPGAN_CHANNEL_MULTIPLIER,
                bg_upsampler=None,
                device=_resolve_device(torch),
            )
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as error:
            raise GFPGANUnavailable(
                "The GFPGAN restoration model could not be initialized."
            ) from error
        _MODEL_CACHE.clear()
        _MODEL_CACHE[cache_key] = restorer
        return restorer


def restore_faces(image):
    """Return a same-size face-restored candidate and non-sensitive metadata."""
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.shape[2] != 3
        or image.size == 0
    ):
        raise GFPGANUnavailable("The GFPGAN input image is invalid.")
    height, width = image.shape[:2]
    if width * height > settings.GFPGAN_MAX_INPUT_PIXELS:
        raise GFPGANUnavailable(
            "The image exceeds the configured GFPGAN inference safety limit."
        )

    restorer = _load_restorer()
    try:
        with _INFERENCE_LOCK:
            _cropped_faces, restored_faces, restored_image = restorer.enhance(
                image,
                has_aligned=False,
                only_center_face=False,
                paste_back=True,
                weight=float(settings.GFPGAN_RESTORATION_WEIGHT),
            )
    except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as error:
        raise GFPGANUnavailable("GFPGAN inference failed safely.") from error

    face_count = 0 if restored_faces is None else len(restored_faces)
    if face_count == 0:
        raise GFPGANUnavailable("No restorable face was detected.")
    if face_count > settings.GFPGAN_MAX_FACES:
        raise GFPGANUnavailable(
            "The detected face count exceeds the configured restoration limit."
        )
    if restored_image is None or not isinstance(restored_image, np.ndarray):
        raise GFPGANUnavailable("GFPGAN returned no restored image.")
    if restored_image.shape != image.shape:
        raise GFPGANUnavailable(
            "GFPGAN changed the image dimensions; the candidate was rejected."
        )
    if not np.isfinite(restored_image).all():
        raise GFPGANUnavailable("GFPGAN returned invalid pixel values.")

    restored_image = np.clip(restored_image, 0, 255).astype(np.uint8)
    return restored_image, {
        "provider": "private_gfpgan_v1_4",
        "faces_restored": face_count,
        "checkpoint_sha256_prefix": settings.GFPGAN_MODEL_SHA256[:12],
        "architecture": settings.GFPGAN_ARCH,
        "restoration_weight": float(settings.GFPGAN_RESTORATION_WEIGHT),
        "background_upsampler": None,
        "dimensions_preserved": True,
    }


def clear_model_cache():
    """Release the cached model; intended for controlled worker shutdown/tests."""
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()

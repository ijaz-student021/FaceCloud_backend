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

from .policy import learned_stage_enabled

from . import runtime


class GFPGANUnavailable(RuntimeError):
    """Raised when private face restoration cannot run safely."""


_MODEL_LOCK = threading.RLock()
# Shared with every other adapter: one heavy inference at a time per process.
_INFERENCE_LOCK = runtime.INFERENCE_LOCK
_MODEL_CACHE = runtime.register_model_cache({})
_NMS_BACKEND = "uninitialized"


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_checkpoint():
    """Validate the private checkpoint and bounded runtime configuration."""
    model_path = Path(settings.GFPGAN_MODEL_PATH).expanduser().resolve()
    expected_sha256 = str(settings.GFPGAN_MODEL_SHA256).strip().lower()
    if not model_path.is_file():
        raise GFPGANUnavailable("The private GFPGAN checkpoint is unavailable.")
    if (
        len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise GFPGANUnavailable("The GFPGAN checkpoint checksum is not configured.")
    if _sha256(model_path) != expected_sha256:
        raise GFPGANUnavailable(
            "The private GFPGAN checkpoint failed integrity verification."
        )

    restoration_weight = float(settings.GFPGAN_RESTORATION_WEIGHT)
    output_blend = float(settings.GFPGAN_OUTPUT_BLEND)
    if not 0.0 <= restoration_weight <= 1.0:
        raise GFPGANUnavailable(
            "GFPGAN_RESTORATION_WEIGHT must be between 0 and 1."
        )
    if not 0.0 <= output_blend <= 1.0:
        raise GFPGANUnavailable("GFPGAN_OUTPUT_BLEND must be between 0 and 1.")
    minimum_blend = float(settings.GFPGAN_MIN_OUTPUT_BLEND)
    if not 0.0 <= minimum_blend <= output_blend:
        raise GFPGANUnavailable(
            "GFPGAN_MIN_OUTPUT_BLEND must be between 0 and GFPGAN_OUTPUT_BLEND."
        )
    if not 0.0 <= float(settings.GFPGAN_MIN_STRUCTURAL_SIMILARITY) <= 1.0:
        raise GFPGANUnavailable(
            "GFPGAN_MIN_STRUCTURAL_SIMILARITY must be between 0 and 1."
        )
    bounded_positive_settings = (
        "GFPGAN_MAX_MEAN_ABSOLUTE_CHANGE",
        "GFPGAN_MAX_P95_ABSOLUTE_CHANGE",
        "GFPGAN_MAX_CHROMA_SHIFT",
        "GFPGAN_MAX_HIGHLIGHT_CLIPPING_GROWTH",
    )
    if any(float(getattr(settings, name)) < 0 for name in bounded_positive_settings):
        raise GFPGANUnavailable("GFPGAN quality-guard limits cannot be negative.")
    if int(settings.GFPGAN_MAX_FACES) < 1:
        raise GFPGANUnavailable("GFPGAN_MAX_FACES must be at least 1.")
    if int(settings.GFPGAN_MAX_INPUT_PIXELS) < 1:
        raise GFPGANUnavailable("GFPGAN_MAX_INPUT_PIXELS must be positive.")
    auxiliary_models = (
        (
            Path(settings.GFPGAN_DETECTION_MODEL_PATH).expanduser().resolve(),
            str(settings.GFPGAN_DETECTION_MODEL_SHA256).strip().lower(),
            "GFPGAN face-detection checkpoint",
        ),
        (
            Path(settings.GFPGAN_PARSING_MODEL_PATH).expanduser().resolve(),
            str(settings.GFPGAN_PARSING_MODEL_SHA256).strip().lower(),
            "GFPGAN face-parsing checkpoint",
        ),
    )
    expected_auxiliary_directory = (
        Path(settings.BASE_DIR) / "gfpgan" / "weights"
    ).resolve()
    for auxiliary_path, auxiliary_sha256, label in auxiliary_models:
        if auxiliary_path.parent != expected_auxiliary_directory:
            raise GFPGANUnavailable(
                f"The {label} must use the pinned local runtime directory."
            )
        if not auxiliary_path.is_file():
            raise GFPGANUnavailable(f"The {label} is unavailable.")
        if (
            len(auxiliary_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in auxiliary_sha256
            )
        ):
            raise GFPGANUnavailable(f"The {label} checksum is not configured.")
        if _sha256(auxiliary_path) != auxiliary_sha256:
            raise GFPGANUnavailable(f"The {label} failed integrity verification.")
    return model_path, expected_sha256, auxiliary_models


def _numpy_nms(detections, threshold):
    """Deterministic NMS fallback when Windows blocks torchvision C++ ops."""
    if detections is None or len(detections) == 0:
        return []
    detections = np.asarray(detections, dtype=np.float32)
    x_min = detections[:, 0]
    y_min = detections[:, 1]
    x_max = detections[:, 2]
    y_max = detections[:, 3]
    scores = detections[:, 4]
    areas = np.maximum(0.0, x_max - x_min + 1) * np.maximum(
        0.0,
        y_max - y_min + 1,
    )
    order = scores.argsort()[::-1]
    kept = []
    while order.size:
        index = int(order[0])
        kept.append(index)
        if order.size == 1:
            break
        remaining = order[1:]
        intersection_x_min = np.maximum(x_min[index], x_min[remaining])
        intersection_y_min = np.maximum(y_min[index], y_min[remaining])
        intersection_x_max = np.minimum(x_max[index], x_max[remaining])
        intersection_y_max = np.minimum(y_max[index], y_max[remaining])
        intersection_width = np.maximum(
            0.0,
            intersection_x_max - intersection_x_min + 1,
        )
        intersection_height = np.maximum(
            0.0,
            intersection_y_max - intersection_y_min + 1,
        )
        intersection = intersection_width * intersection_height
        union = areas[index] + areas[remaining] - intersection
        overlap = intersection / np.maximum(union, 1e-6)
        order = remaining[overlap <= threshold]
    return kept


def _configure_nms_backend(torch):
    global _NMS_BACKEND
    try:
        import torchvision

        torchvision.ops.nms(
            boxes=torch.empty((0, 4), dtype=torch.float32),
            scores=torch.empty((0,), dtype=torch.float32),
            iou_threshold=0.4,
        )
        _NMS_BACKEND = "torchvision_compiled"
    except (ImportError, OSError, RuntimeError, AttributeError):
        try:
            from facexlib.detection import retinaface
            from facexlib.detection import retinaface_utils
        except (ImportError, OSError) as error:
            raise GFPGANUnavailable(
                "The GFPGAN face-detection runtime is unavailable."
            ) from error
        retinaface.py_cpu_nms = _numpy_nms
        retinaface_utils.py_cpu_nms = _numpy_nms
        _NMS_BACKEND = "numpy_application_control_fallback"


def _restore_removed_torchvision_module():
    """Give BasicSR the one torchvision module it imports that no longer exists.

    BasicSR 1.4.2 (which GFPGAN 1.3.8 depends on) imports ``rgb_to_grayscale``
    from ``torchvision.transforms.functional_tensor``. torchvision removed that
    private module in 0.17 and exposes the same function from
    ``torchvision.transforms.functional``. Django 6 needs Python 3.12, for which
    no torchvision older than 0.17 exists, so the old module cannot simply be
    installed. Only the missing name is supplied, and only when it is missing.
    """
    import importlib
    import sys
    import types

    name = "torchvision.transforms.functional_tensor"
    if name in sys.modules:
        return False
    try:
        importlib.import_module(name)
        return False
    except ImportError:
        pass
    from torchvision.transforms import functional

    shim = types.ModuleType(name)
    shim.rgb_to_grayscale = functional.rgb_to_grayscale
    sys.modules[name] = shim
    return True


def _import_runtime():
    try:
        import torch
        _restore_removed_torchvision_module()
        from gfpgan import GFPGANer
    except (ImportError, OSError) as error:
        raise GFPGANUnavailable(
            "The isolated GFPGAN runtime is unavailable."
        ) from error
    _configure_nms_backend(torch)
    return torch, GFPGANer


def _resolve_device(torch):
    configured = str(settings.GFPGAN_DEVICE).strip().lower()
    if configured == "auto":
        if torch.cuda.is_available():
            configured = "cuda"
        elif torch.backends.mps.is_available():
            configured = "mps"
        else:
            configured = "cpu"
    if configured == "cuda" and not torch.cuda.is_available():
        raise GFPGANUnavailable("The configured GFPGAN CUDA device is unavailable.")
    if configured == "mps" and not torch.backends.mps.is_available():
        raise GFPGANUnavailable("The configured GFPGAN MPS device is unavailable.")
    try:
        return torch.device(configured)
    except (RuntimeError, ValueError) as error:
        raise GFPGANUnavailable("The configured GFPGAN device is invalid.") from error


def _load_restorer():
    model_path, expected_sha256, auxiliary_models = _validated_checkpoint()

    stat = model_path.stat()
    cache_key = (
        str(model_path),
        stat.st_size,
        stat.st_mtime_ns,
        expected_sha256,
        settings.GFPGAN_ARCH,
        settings.GFPGAN_CHANNEL_MULTIPLIER,
        settings.GFPGAN_DEVICE,
        tuple(
            (
                str(path),
                path.stat().st_size,
                path.stat().st_mtime_ns,
                sha256,
            )
            for path, sha256, _label in auxiliary_models
        ),
    )
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached
        torch, restorer_class = _import_runtime()
        runtime.make_room(_MODEL_CACHE)
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
        or image.dtype != np.uint8
    ):
        raise GFPGANUnavailable("The GFPGAN input image is invalid.")
    image = np.ascontiguousarray(image)
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
    except (RuntimeError, ValueError, TypeError, AttributeError, OSError, MemoryError) as error:
        if runtime.is_out_of_memory(error):
            # Faces are restored as fixed 512px crops, so there is no smaller tile
            # to retry with: free the device and keep the unrestored image.
            runtime.release_memory(getattr(restorer, "device", "cpu"))
            raise GFPGANUnavailable("GFPGAN ran out of memory; the faces were left as they were.") from error
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
        "device": str(getattr(restorer, "device", settings.GFPGAN_DEVICE)),
    }


def gfpgan_healthcheck(load_model=False):
    """Return non-sensitive readiness metadata or raise a safe error."""
    model_path, expected_sha256, auxiliary_models = _validated_checkpoint()
    metadata = {
        "enabled": learned_stage_enabled("GFPGAN_ENABLED"),
        "configured": bool(settings.GFPGAN_ENABLED),
        "queue_enabled": bool(settings.GFPGAN_QUEUE_ENABLED),
        "queue_name": str(settings.GFPGAN_QUEUE_NAME),
        "checkpoint": model_path.name,
        "checkpoint_size_bytes": model_path.stat().st_size,
        "checkpoint_sha256_prefix": expected_sha256[:12],
        "device": str(settings.GFPGAN_DEVICE),
        "architecture": str(settings.GFPGAN_ARCH),
        "runtime_loaded": False,
        "nms_backend": _NMS_BACKEND,
        "auxiliary_checkpoints": [
            {
                "checkpoint": path.name,
                "checkpoint_size_bytes": path.stat().st_size,
                "checkpoint_sha256_prefix": sha256[:12],
            }
            for path, sha256, _label in auxiliary_models
        ],
    }
    if load_model:
        _load_restorer()
        metadata["runtime_loaded"] = True
        metadata["nms_backend"] = _NMS_BACKEND
    return metadata


def clear_model_cache():
    """Release the cached model; intended for controlled worker shutdown/tests."""
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()

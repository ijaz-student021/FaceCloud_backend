"""CodeFormer face restoration: a checksum-pinned local PyTorch adapter.

Same contract as ``gfpgan.restore_faces``: ``restore_faces_codeformer(image)`` returns a candidate
of the same size as ``image`` plus metadata, and raises only ``CodeFormerUnavailable`` (a
``GFPGANUnavailable``, so the pipeline's existing handlers apply). The candidate then passes through the
pipeline's own face-restoration guard and the identity gate, exactly as a GFPGAN candidate does.

Faces are found with the configured detector (YuNet by default, RetinaFace when selected), aligned to the
512 px FFHQ template with a similarity transform from the five landmarks, restored, and pasted back with a
feathered mask. Everything is OpenCV and PyTorch: no facexlib or basicsr is needed, so it runs in the main
environment as well as the isolated worker.

``fidelity`` is CodeFormer's ``w``: 0 favours quality (more invented detail), 1 favours fidelity to the input.
Licence: S-Lab License 1.0, non-commercial use only (docs/model-licenses/codeformer.md).
"""
import hashlib
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

from . import runtime
from .gfpgan import GFPGANUnavailable


class CodeFormerUnavailable(GFPGANUnavailable):
    """Raised when CodeFormer cannot run safely."""


_MODEL_CACHE = {}

# The 5-point FFHQ template for a 512 px aligned face (the one CodeFormer was trained on).
_TEMPLATE_512 = np.array(
    [[192.98138, 239.94708], [318.90277, 240.1936], [256.63416, 314.01935], [201.26117, 371.41043], [313.08905, 371.15118]],
    dtype=np.float32,
)
_SIZE = 512


def _torch():
    try:
        import torch
    except ImportError as error:
        raise CodeFormerUnavailable("The PyTorch runtime is unavailable.") from error
    return torch


def _verified_path():
    path = Path(settings.CODEFORMER_MODEL_PATH).expanduser().resolve()
    if not path.is_file():
        raise CodeFormerUnavailable("The CodeFormer checkpoint is unavailable.")
    expected = str(settings.CODEFORMER_MODEL_SHA256).strip().lower()
    if len(expected) != 64:
        raise CodeFormerUnavailable("The CodeFormer checksum is not configured.")
    hasher = hashlib.sha256()
    with path.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != expected:
        raise CodeFormerUnavailable("The CodeFormer checkpoint failed integrity verification.")
    return path


def _load_model():
    torch = _torch()
    path = _verified_path()
    device = runtime.resolve_device(torch, settings.CODEFORMER_DEVICE, "CodeFormer", CodeFormerUnavailable)
    key = (str(path), str(device))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    from .archs.codeformer import CodeFormer

    model = CodeFormer(dim_embd=512, codebook_size=1024, n_head=8, n_layers=9, connect_list=["32", "64", "128", "256"])
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        model.load_state_dict(checkpoint["params_ema"], strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise CodeFormerUnavailable("The CodeFormer checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


def _detect(image):
    from validation.services import OpenCVYuNetFaceDetector, get_face_detector

    try:
        detection = get_face_detector().detect(image)
        if len(detection.boxes) and not any(len(points) == 5 for points in detection.landmarks):
            detection = OpenCVYuNetFaceDetector().detect(image)  # the configured detector gave no landmarks
    except Exception as error:  # a detector failure is a restoration failure, never a crash
        raise CodeFormerUnavailable("Face detection for CodeFormer failed safely.") from error
    faces = []
    for box, points in zip(detection.boxes, detection.landmarks):
        if len(points) == 5:
            faces.append((box, np.array(points, dtype=np.float32)))
    faces.sort(key=lambda item: item[0][2] * item[0][3], reverse=True)
    return faces[: max(1, int(settings.CODEFORMER_MAX_FACES))]


def _feather_mask():
    mask = np.zeros((_SIZE, _SIZE), np.float32)
    cv2.ellipse(mask, (256, 285), (176, 214), 0, 0, 360, 1.0, -1)
    return cv2.GaussianBlur(mask, (0, 0), 24)


_MASK = None


def _restore_one(model, device, torch, image, landmarks, fidelity):
    matrix, _inliers = cv2.estimateAffinePartial2D(landmarks, _TEMPLATE_512, method=cv2.LMEDS)
    if matrix is None:
        raise CodeFormerUnavailable("A face could not be aligned for CodeFormer.")
    crop = cv2.warpAffine(image, matrix, (_SIZE, _SIZE), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=(135, 133, 132))
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).unsqueeze(0)
    tensor = ((tensor - 0.5) / 0.5).to(device)
    with torch.no_grad():
        restored = model(tensor, w=fidelity, adain=True)[0]
    restored = (restored.clamp(-1, 1) * 0.5 + 0.5).squeeze(0).cpu().numpy().transpose(1, 2, 0)
    restored = cv2.cvtColor(np.rint(restored * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    return restored, matrix


def restore_faces_codeformer(image, fidelity=None):
    """Return ``(candidate, metadata)``; ``candidate`` has the size of ``image``."""
    global _MASK
    if image is None or image.size == 0:
        raise CodeFormerUnavailable("The CodeFormer input image is invalid.")
    fidelity = float(settings.CODEFORMER_FIDELITY if fidelity is None else fidelity)
    if not 0.0 <= fidelity <= 1.0:
        raise CodeFormerUnavailable("CODEFORMER_FIDELITY must be between 0 and 1.")
    pixels = image.shape[0] * image.shape[1]
    if pixels > int(settings.CODEFORMER_MAX_INPUT_PIXELS):
        raise CodeFormerUnavailable("The image exceeds the CodeFormer input safety limit.")
    faces = _detect(image)
    if not faces:
        raise CodeFormerUnavailable("No face with landmarks was found for CodeFormer.")
    torch = _torch()
    if _MASK is None:
        _MASK = _feather_mask()
    height, width = image.shape[:2]
    output = image.astype(np.float32)
    with runtime.INFERENCE_LOCK:
        model, device = _load_model()
        restored_count = 0
        for _box, landmarks in faces:
            try:
                restored, matrix = _restore_one(model, device, torch, image, landmarks, fidelity)
            except CodeFormerUnavailable:
                raise
            except Exception as error:
                if runtime.is_out_of_memory(error):
                    runtime.release_memory(device)
                    raise CodeFormerUnavailable(f"CodeFormer ran out of memory on {device}; the image was left as it was.") from error
                if isinstance(error, (RuntimeError, ValueError, TypeError, cv2.error)):
                    raise CodeFormerUnavailable("CodeFormer inference failed safely.") from error
                raise
            inverse = cv2.invertAffineTransform(matrix)
            face = cv2.warpAffine(restored, inverse, (width, height), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0)).astype(np.float32)
            mask = cv2.warpAffine(_MASK, inverse, (width, height), flags=cv2.INTER_LINEAR)[..., None]
            output = output * (1.0 - mask) + face * mask
            restored_count += 1
    return np.clip(np.rint(output), 0, 255).astype(np.uint8), {
        "provider": "codeformer_v0_1",
        "checkpoint_sha256_prefix": settings.CODEFORMER_MODEL_SHA256[:12],
        "device": str(device),
        "fidelity": fidelity,
        "faces_detected": len(faces),
        "faces_restored": restored_count,
    }

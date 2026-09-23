"""Checksum-pinned local PureVision (old-arch RRDBNet) artifact-removal adapter.

PureVision is a third-party, non-commercially licensed (CC BY-NC-SA 4.0)
checkpoint from https://github.com/limitlesslab/AI-upscaling-models. It runs
at native (1x) resolution: the input is pixel-unshuffled by 4 before the RRDB
trunk and the network's internal 4x upsampling exactly cancels that
unshuffle, so the output pixel grid always matches the input. This makes it
safe to run before any resolution-changing stage (unlike PureScale/
Real-ESRGAN), and it targets the same role NAFNet was meant to fill:
removing compression artifacts and mild blur without inventing detail.
"""

import hashlib
import threading
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings


class PureVisionUnavailable(RuntimeError):
    """Raised when the PureVision restorer cannot run safely."""


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE = {}

_NUM_FILTERS = 64
_GROWTH_CHANNELS = 32
_NUM_BLOCKS = 23
_SHUFFLE_FACTOR = 4


def _runtime():
    try:
        import torch
        from torch import nn
        from torch.nn import functional as functional
    except ImportError as error:
        raise PureVisionUnavailable("The isolated PyTorch runtime is unavailable.") from error
    return torch, nn, functional


def _verified_path(path_value, expected_hash, label):
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise PureVisionUnavailable(f"The {label} checkpoint is unavailable.")
    expected = str(expected_hash).strip().lower()
    if len(expected) != 64:
        raise PureVisionUnavailable(f"The {label} checksum is not configured.")
    hasher = hashlib.sha256()
    with path.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise PureVisionUnavailable(f"The {label} checkpoint failed integrity verification.")
    return path


def _auto_device(torch):
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _device(torch):
    configured = str(settings.PUREVISION_DEVICE).strip().lower()
    if configured == "auto":
        configured = _auto_device(torch)
    if configured.startswith("cuda") and not torch.cuda.is_available():
        raise PureVisionUnavailable("The configured PureVision CUDA device is unavailable.")
    if configured == "mps" and not torch.backends.mps.is_available():
        raise PureVisionUnavailable("The configured PureVision MPS device is unavailable.")
    try:
        return torch.device(configured)
    except (RuntimeError, ValueError) as error:
        raise PureVisionUnavailable("The configured PureVision device is invalid.") from error


def _build_purevision(nn, functional):
    torch, _nn, _functional = _runtime()
    nf, gc, nb, shuffle_factor = _NUM_FILTERS, _GROWTH_CHANNELS, _NUM_BLOCKS, _SHUFFLE_FACTOR

    class ResidualDenseBlock5C(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Sequential(nn.Conv2d(nf, gc, 3, 1, 1), nn.LeakyReLU(0.2, inplace=True))
            self.conv2 = nn.Sequential(nn.Conv2d(nf + gc, gc, 3, 1, 1), nn.LeakyReLU(0.2, inplace=True))
            self.conv3 = nn.Sequential(nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1), nn.LeakyReLU(0.2, inplace=True))
            self.conv4 = nn.Sequential(nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1), nn.LeakyReLU(0.2, inplace=True))
            self.conv5 = nn.Sequential(nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1))

        def forward(self, value):
            x1 = self.conv1(value)
            x2 = self.conv2(torch.cat((value, x1), 1))
            x3 = self.conv3(torch.cat((value, x1, x2), 1))
            x4 = self.conv4(torch.cat((value, x1, x2, x3), 1))
            x5 = self.conv5(torch.cat((value, x1, x2, x3, x4), 1))
            return x5 * 0.2 + value

    class RRDBOld(nn.Module):
        def __init__(self):
            super().__init__()
            self.RDB1 = ResidualDenseBlock5C()
            self.RDB2 = ResidualDenseBlock5C()
            self.RDB3 = ResidualDenseBlock5C()

        def forward(self, value):
            out = self.RDB1(value)
            out = self.RDB2(out)
            out = self.RDB3(out)
            return out * 0.2 + value

    class ShortcutBlock(nn.Module):
        def __init__(self, submodule):
            super().__init__()
            self.sub = submodule

        def forward(self, value):
            return value + self.sub(value)

    class PureVisionNet(nn.Module):
        def __init__(self):
            super().__init__()
            trunk = nn.Sequential(*(RRDBOld() for _ in range(nb)), nn.Conv2d(nf, nf, 3, 1, 1))
            self.model = nn.Sequential(
                nn.Conv2d(3 * shuffle_factor**2, nf, 3, 1, 1),
                ShortcutBlock(trunk),
                nn.Upsample(scale_factor=2, mode="nearest"),
                nn.Conv2d(nf, nf, 3, 1, 1),
                nn.LeakyReLU(0.2, inplace=True),
                nn.Upsample(scale_factor=2, mode="nearest"),
                nn.Conv2d(nf, nf, 3, 1, 1),
                nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(nf, nf, 3, 1, 1),
                nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(nf, 3, 3, 1, 1),
            )

        def forward(self, value):
            unshuffled = functional.pixel_unshuffle(value, shuffle_factor)
            return self.model(unshuffled)

    return PureVisionNet()


def _load_model():
    path = _verified_path(settings.PUREVISION_MODEL_PATH, settings.PUREVISION_MODEL_SHA256, "PureVision")
    torch, nn, functional = _runtime()
    device = _device(torch)
    key = (str(path), str(device))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    model = _build_purevision(nn, functional)
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        state = checkpoint.get("params_ema", checkpoint.get("params", checkpoint))
        model.load_state_dict(state, strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise PureVisionUnavailable("The PureVision checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


def _tensor_from_bgr(image, torch, device):
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).unsqueeze(0).to(device)


def _bgr_from_tensor(tensor):
    rgb = tensor.detach().float().clamp_(0, 1).squeeze(0).cpu().numpy().transpose(1, 2, 0)
    return cv2.cvtColor(np.rint(rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def _tiled_inference(image, model, device, tile, overlap):
    torch, _nn, _functional = _runtime()
    height, width = image.shape[:2]
    output = np.zeros((height, width, 3), dtype=np.float32)
    weights = np.zeros((height, width, 1), dtype=np.float32)
    step = max(1, tile - overlap)
    y_positions = list(range(0, max(height - tile, 0) + 1, step))
    x_positions = list(range(0, max(width - tile, 0) + 1, step))
    if not y_positions or y_positions[-1] != max(height - tile, 0):
        y_positions.append(max(height - tile, 0))
    if not x_positions or x_positions[-1] != max(width - tile, 0):
        x_positions.append(max(width - tile, 0))
    with torch.inference_mode():
        for y in y_positions:
            for x in x_positions:
                patch = image[y:min(y + tile, height), x:min(x + tile, width)]
                prediction = _bgr_from_tensor(model(_tensor_from_bgr(patch, torch, device)))
                ph, pw = prediction.shape[:2]
                output[y:y + ph, x:x + pw] += prediction.astype(np.float32)
                weights[y:y + ph, x:x + pw] += 1.0
    return np.rint(output / np.maximum(weights, 1.0)).astype(np.uint8)


def restore_with_purevision(image):
    if image is None or image.size == 0:
        raise PureVisionUnavailable("The PureVision input image is invalid.")
    with _MODEL_LOCK:
        model, device = _load_model()
        output = _tiled_inference(
            image, model, device,
            settings.PUREVISION_TILE_SIZE, settings.PUREVISION_TILE_OVERLAP,
        )
    source_lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    output_lab = cv2.cvtColor(output, cv2.COLOR_BGR2LAB).astype(np.float32)
    chroma_shift = float(np.mean(np.abs(output_lab[:, :, 1:] - source_lab[:, :, 1:])))
    luminance_shift = float(np.mean(np.abs(output_lab[:, :, 0] - source_lab[:, :, 0])))
    if (
        chroma_shift > settings.PUREVISION_MAX_MEAN_CHROMA_SHIFT
        or luminance_shift > settings.PUREVISION_MAX_MEAN_LUMINANCE_SHIFT
    ):
        raise PureVisionUnavailable("The PureVision candidate failed the image-fidelity guard.")
    return output, {
        "provider": "purevision_rrdb_1x",
        "checkpoint_sha256_prefix": settings.PUREVISION_MODEL_SHA256[:12],
        "license": "CC BY-NC-SA 4.0 (non-commercial)",
    }

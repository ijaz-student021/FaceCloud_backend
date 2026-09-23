"""Checksum-pinned local PureScale (SAFMN) 2x super-resolution adapter.

PureScale is a third-party, non-commercially licensed (CC BY-NC-SA 4.0)
checkpoint from https://github.com/limitlesslab/AI-upscaling-models. It is an
alternative super-resolution candidate to Real-ESRGAN and must not run in the
same pass as ``hd_restoration_stage`` (see ``purescale_super_resolution_stage``
in ``services.py``), since stacking two independent upscalers would compound
scale factors and could exceed the output-pixel safety limit.
"""

import hashlib
import threading
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings


class PureScaleUnavailable(RuntimeError):
    """Raised when the PureScale upscaler cannot run safely."""


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE = {}


def _runtime():
    try:
        import torch
        from torch import nn
        from torch.nn import functional as functional
    except ImportError as error:
        raise PureScaleUnavailable("The isolated PyTorch runtime is unavailable.") from error
    return torch, nn, functional


def _verified_path(path_value, expected_hash, label):
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise PureScaleUnavailable(f"The {label} checkpoint is unavailable.")
    expected = str(expected_hash).strip().lower()
    if len(expected) != 64:
        raise PureScaleUnavailable(f"The {label} checksum is not configured.")
    hasher = hashlib.sha256()
    with path.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise PureScaleUnavailable(f"The {label} checkpoint failed integrity verification.")
    return path


def _auto_device(torch):
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _device(torch):
    configured = str(settings.PURESCALE_DEVICE).strip().lower()
    if configured == "auto":
        configured = _auto_device(torch)
    if configured.startswith("cuda") and not torch.cuda.is_available():
        raise PureScaleUnavailable("The configured PureScale CUDA device is unavailable.")
    if configured == "mps" and not torch.backends.mps.is_available():
        raise PureScaleUnavailable("The configured PureScale MPS device is unavailable.")
    try:
        return torch.device(configured)
    except (RuntimeError, ValueError) as error:
        raise PureScaleUnavailable("The configured PureScale device is invalid.") from error


def _build_safmn(nn, functional):
    torch, _nn, _functional = _runtime()
    dim = 128
    n_blocks = 16
    ffn_scale = 2.0
    upscaling_factor = 2

    class LayerNorm(nn.Module):
        def __init__(self, channels, eps=1e-6):
            super().__init__()
            self.weight = nn.Parameter(torch.ones(channels))
            self.bias = nn.Parameter(torch.zeros(channels))
            self.eps = eps

        def forward(self, value):
            mean = value.mean(1, keepdim=True)
            variance = (value - mean).pow(2).mean(1, keepdim=True)
            normalized = (value - mean) / torch.sqrt(variance + self.eps)
            return self.weight[:, None, None] * normalized + self.bias[:, None, None]

    class SAFM(nn.Module):
        def __init__(self, channels, n_levels=4):
            super().__init__()
            self.n_levels = n_levels
            chunk_dim = channels // n_levels
            self.mfr = nn.ModuleList(
                nn.Conv2d(chunk_dim, chunk_dim, 3, 1, 1, groups=chunk_dim)
                for _ in range(n_levels)
            )
            self.aggr = nn.Conv2d(channels, channels, 1, 1, 0)
            self.act = nn.GELU()

        def forward(self, value):
            height, width = value.shape[-2:]
            chunks = value.chunk(self.n_levels, dim=1)
            outputs = []
            for index in range(self.n_levels):
                if index > 0:
                    pooled_size = (height // (2**index), width // (2**index))
                    pooled = functional.adaptive_avg_pool2d(chunks[index], pooled_size)
                    pooled = self.mfr[index](pooled)
                    pooled = functional.interpolate(pooled, size=(height, width), mode="bicubic")
                else:
                    pooled = self.mfr[index](chunks[index])
                outputs.append(pooled)
            aggregated = self.aggr(torch.cat(outputs, dim=1))
            return self.act(aggregated) * value

    class CCM(nn.Module):
        def __init__(self, channels, growth_rate):
            super().__init__()
            hidden = int(channels * growth_rate)
            self.ccm = nn.Sequential(
                nn.Conv2d(channels, hidden, 3, 1, 1),
                nn.GELU(),
                nn.Conv2d(hidden, channels, 1, 1, 0),
            )

        def forward(self, value):
            return self.ccm(value)

    class AttBlock(nn.Module):
        def __init__(self, channels, block_ffn_scale):
            super().__init__()
            self.norm1 = LayerNorm(channels)
            self.norm2 = LayerNorm(channels)
            self.safm = SAFM(channels)
            self.ccm = CCM(channels, block_ffn_scale)

        def forward(self, value):
            value = self.safm(self.norm1(value)) + value
            value = self.ccm(self.norm2(value)) + value
            return value

    class SAFMN(nn.Module):
        def __init__(self):
            super().__init__()
            self.to_feat = nn.Conv2d(3, dim, 3, 1, 1)
            self.feats = nn.Sequential(*[AttBlock(dim, ffn_scale) for _ in range(n_blocks)])
            self.to_img = nn.Sequential(
                nn.Conv2d(dim, 3 * upscaling_factor**2, 3, 1, 1),
                nn.PixelShuffle(upscaling_factor),
            )

        def forward(self, value):
            value = self.to_feat(value)
            value = self.feats(value) + value
            return self.to_img(value)

    return SAFMN()


def _load_model():
    path = _verified_path(settings.PURESCALE_MODEL_PATH, settings.PURESCALE_MODEL_SHA256, "PureScale")
    torch, nn, functional = _runtime()
    device = _device(torch)
    key = (str(path), str(device))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    model = _build_safmn(nn, functional)
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        state = checkpoint.get("params_ema", checkpoint.get("params", checkpoint))
        model.load_state_dict(state, strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise PureScaleUnavailable("The PureScale checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


def _tensor_from_bgr(image, torch, device):
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).unsqueeze(0).to(device)


def _bgr_from_tensor(tensor):
    rgb = tensor.detach().float().clamp_(0, 1).squeeze(0).cpu().numpy().transpose(1, 2, 0)
    return cv2.cvtColor(np.rint(rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def _tiled_inference(image, model, device, scale, tile, overlap):
    torch, _nn, _functional = _runtime()
    height, width = image.shape[:2]
    output = np.zeros((height * scale, width * scale, 3), dtype=np.float32)
    weights = np.zeros((height * scale, width * scale, 1), dtype=np.float32)
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
                oy, ox = y * scale, x * scale
                output[oy:oy + ph, ox:ox + pw] += prediction.astype(np.float32)
                weights[oy:oy + ph, ox:ox + pw] += 1.0
    return np.rint(output / np.maximum(weights, 1.0)).astype(np.uint8)


def upscale_with_purescale(image):
    if image is None or image.size == 0:
        raise PureScaleUnavailable("The PureScale input image is invalid.")
    height, width = image.shape[:2]
    output_pixels = height * width * 4
    if output_pixels > settings.PURESCALE_MAX_OUTPUT_PIXELS:
        raise PureScaleUnavailable("The requested PureScale output exceeds the safety limit.")
    with _MODEL_LOCK:
        model, device = _load_model()
        output = _tiled_inference(
            image, model, device, 2,
            settings.PURESCALE_TILE_SIZE, settings.PURESCALE_TILE_OVERLAP,
        )
    return output, {
        "provider": "purescale_safmn_2x",
        "native_scale": 2,
        "checkpoint_sha256_prefix": settings.PURESCALE_MODEL_SHA256[:12],
        "license": "CC BY-NC-SA 4.0 (non-commercial)",
    }

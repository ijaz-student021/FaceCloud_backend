"""Checksum-pinned local NAFNet and Real-ESRGAN inference adapters."""

import hashlib
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

from . import runtime


class HDRestorationUnavailable(RuntimeError):
    """Raised when an optional HD model cannot run safely."""


# Shared with every other adapter: one heavy inference at a time per process.
_MODEL_LOCK = runtime.INFERENCE_LOCK
_MODEL_CACHE = {}


def _runtime():
    try:
        import torch
        from torch import nn
        from torch.nn import functional as functional
    except ImportError as error:
        raise HDRestorationUnavailable("The isolated PyTorch runtime is unavailable.") from error
    return torch, nn, functional


def _verified_path(path_value, expected_hash, label):
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise HDRestorationUnavailable(f"The {label} checkpoint is unavailable.")
    expected = str(expected_hash).strip().lower()
    if len(expected) != 64:
        raise HDRestorationUnavailable(f"The {label} checksum is not configured.")
    hasher = hashlib.sha256()
    with path.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise HDRestorationUnavailable(f"The {label} checkpoint failed integrity verification.")
    return path


def _device(torch):
    return runtime.resolve_device(
        torch, settings.HD_RESTORATION_DEVICE, "HD-restoration", HDRestorationUnavailable,
    )


# NAFNet variants (megvii-research/NAFNet, MIT): block counts per encoder stage, middle and decoder stage.
NAFNET_CONFIGS = {
    "nafnet": {"enc": (1, 1, 1, 28), "middle": 1, "dec": (1, 1, 1, 1)},        # GoPro-width64: motion deblurring
    "nafnet_sidd": {"enc": (2, 2, 4, 8), "middle": 12, "dec": (2, 2, 2, 2)},   # SIDD-width64: real-noise denoising
}


def _build_nafnet(nn, functional, kind="nafnet"):
    config = NAFNET_CONFIGS[kind]
    torch, _nn, _functional = _runtime()

    class LayerNorm2d(nn.Module):
        def __init__(self, channels):
            super().__init__()
            self.weight = nn.Parameter(torch.ones(channels))
            self.bias = nn.Parameter(torch.zeros(channels))

        def forward(self, value):
            mean = value.mean(1, keepdim=True)
            variance = (value - mean).pow(2).mean(1, keepdim=True)
            normalized = (value - mean) / (variance + 1e-6).sqrt()
            return normalized * self.weight[:, None, None] + self.bias[:, None, None]

    class SimpleGate(nn.Module):
        def forward(self, value):
            first, second = value.chunk(2, dim=1)
            return first * second

    class NAFBlock(nn.Module):
        def __init__(self, channels):
            super().__init__()
            expanded = channels * 2
            self.conv1 = nn.Conv2d(channels, expanded, 1)
            self.conv2 = nn.Conv2d(expanded, expanded, 3, padding=1, groups=expanded)
            self.conv3 = nn.Conv2d(channels, channels, 1)
            self.sca = nn.Sequential(nn.AdaptiveAvgPool2d(1), nn.Conv2d(channels, channels, 1))
            self.sg = SimpleGate()
            self.conv4 = nn.Conv2d(channels, expanded, 1)
            self.conv5 = nn.Conv2d(channels, channels, 1)
            self.norm1 = LayerNorm2d(channels)
            self.norm2 = LayerNorm2d(channels)
            self.dropout1 = nn.Identity()
            self.dropout2 = nn.Identity()
            self.beta = nn.Parameter(torch.zeros((1, channels, 1, 1)))
            self.gamma = nn.Parameter(torch.zeros((1, channels, 1, 1)))

        def forward(self, source):
            value = self.sg(self.conv2(self.conv1(self.norm1(source))))
            # Simplified channel attention *re-weights* the features by their
            # pooled summary. Feeding the pooled summary on in their place (as
            # this did) leaves one value per channel for the whole tile, which is
            # what produced the washed-out, colour-shifted output that failed QA.
            value = self.conv3(value * self.sca(value))
            intermediate = source + value * self.beta
            value = self.conv5(self.sg(self.conv4(self.norm2(intermediate))))
            return intermediate + value * self.gamma

    class NAFNet(nn.Module):
        def __init__(self):
            super().__init__()
            width = 64
            self.intro = nn.Conv2d(3, width, 3, padding=1)
            self.ending = nn.Conv2d(width, 3, 3, padding=1)
            self.encoders = nn.ModuleList()
            self.decoders = nn.ModuleList()
            self.downs = nn.ModuleList()
            self.ups = nn.ModuleList()
            channels = width
            for count in config["enc"]:
                self.encoders.append(nn.Sequential(*[NAFBlock(channels) for _ in range(count)]))
                self.downs.append(nn.Conv2d(channels, channels * 2, 2, 2))
                channels *= 2
            self.middle_blks = nn.Sequential(*[NAFBlock(channels) for _ in range(config["middle"])])
            for count in config["dec"]:
                self.ups.append(nn.Sequential(nn.Conv2d(channels, channels * 2, 1, bias=False), nn.PixelShuffle(2)))
                channels //= 2
                self.decoders.append(nn.Sequential(*[NAFBlock(channels) for _ in range(count)]))
            self.padder_size = 16

        def forward(self, source):
            height, width = source.shape[-2:]
            value = functional.pad(source, (0, (-width) % 16, 0, (-height) % 16))
            padded_source = value
            value = self.intro(value)
            skips = []
            for encoder, down in zip(self.encoders, self.downs):
                value = encoder(value)
                skips.append(value)
                value = down(value)
            value = self.middle_blks(value)
            for decoder, up, skip in zip(self.decoders, self.ups, reversed(skips)):
                value = decoder(up(value) + skip)
            value = self.ending(value) + padded_source
            return value[:, :, :height, :width]

    return NAFNet()


def _build_realesrgan(nn, functional):
    class RDB(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(64, 32, 3, 1, 1)
            self.conv2 = nn.Conv2d(96, 32, 3, 1, 1)
            self.conv3 = nn.Conv2d(128, 32, 3, 1, 1)
            self.conv4 = nn.Conv2d(160, 32, 3, 1, 1)
            self.conv5 = nn.Conv2d(192, 64, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, source):
            x1 = self.lrelu(self.conv1(source))
            x2 = self.lrelu(self.conv2(np_cat((source, x1))))
            x3 = self.lrelu(self.conv3(np_cat((source, x1, x2))))
            x4 = self.lrelu(self.conv4(np_cat((source, x1, x2, x3))))
            x5 = self.conv5(np_cat((source, x1, x2, x3, x4)))
            return source + x5 * 0.2

    def np_cat(values):
        torch, _nn, _functional = _runtime()
        return torch.cat(values, 1)

    class RRDB(nn.Module):
        def __init__(self):
            super().__init__()
            self.rdb1, self.rdb2, self.rdb3 = RDB(), RDB(), RDB()

        def forward(self, source):
            return source + self.rdb3(self.rdb2(self.rdb1(source))) * 0.2

    class RRDBNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv_first = nn.Conv2d(3, 64, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB() for _ in range(23)])
            self.conv_body = nn.Conv2d(64, 64, 3, 1, 1)
            self.conv_up1 = nn.Conv2d(64, 64, 3, 1, 1)
            self.conv_up2 = nn.Conv2d(64, 64, 3, 1, 1)
            self.conv_hr = nn.Conv2d(64, 64, 3, 1, 1)
            self.conv_last = nn.Conv2d(64, 3, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, source):
            first = self.conv_first(source)
            value = first + self.conv_body(self.body(first))
            value = self.lrelu(self.conv_up1(functional.interpolate(value, scale_factor=2, mode="nearest")))
            value = self.lrelu(self.conv_up2(functional.interpolate(value, scale_factor=2, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(value)))

    return RRDBNet()


def _load_model(kind):
    torch, nn, functional = _runtime()
    if kind == "nafnet":
        path = _verified_path(settings.NAFNET_MODEL_PATH, settings.NAFNET_MODEL_SHA256, "NAFNet")
        builder = _build_nafnet
    elif kind == "nafnet_sidd":
        path = _verified_path(settings.NAFNET_SIDD_MODEL_PATH, settings.NAFNET_SIDD_MODEL_SHA256, "NAFNet-SIDD")
        builder = lambda nn_, functional_: _build_nafnet(nn_, functional_, "nafnet_sidd")  # noqa: E731
    else:
        path = _verified_path(settings.REALESRGAN_MODEL_PATH, settings.REALESRGAN_MODEL_SHA256, "Real-ESRGAN")
        builder = _build_realesrgan
    device = _device(torch)
    key = (kind, str(path), str(device))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    model = builder(nn, functional)
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        state = checkpoint.get("params_ema", checkpoint.get("params", checkpoint))
        model.load_state_dict(state, strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise HDRestorationUnavailable(f"The {kind} checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


def _tiled_inference(image, model, device, scale, tile, overlap, output_scale=None):
    torch, _nn, _functional = _runtime()
    return runtime.tiled_inference(torch, image, model, device, scale, tile, overlap, output_scale)


def _infer(kind, label, image, scale, output_scale=None):
    """Load the model and run it tile by tile, shrinking tiles if memory runs out."""
    with _MODEL_LOCK:
        model, device = _load_model(kind)
        return runtime.run_with_memory_fallback(
            lambda tile, overlap: _tiled_inference(image, model, device, scale, tile, overlap, output_scale),
            device=device,
            tile=settings.HD_RESTORATION_TILE_SIZE, overlap=settings.HD_RESTORATION_TILE_OVERLAP,
            label=label, error_class=HDRestorationUnavailable,
        )


def deblur_with_nafnet(image):
    if image is None or image.size == 0:
        raise HDRestorationUnavailable("The NAFNet input image is invalid.")
    output, inference = _infer("nafnet", "NAFNet", image, 1)
    source_lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    output_lab = cv2.cvtColor(output, cv2.COLOR_BGR2LAB).astype(np.float32)
    chroma_shift = float(np.mean(np.abs(output_lab[:, :, 1:] - source_lab[:, :, 1:])))
    luminance_shift = float(np.mean(np.abs(output_lab[:, :, 0] - source_lab[:, :, 0])))
    if (
        chroma_shift > settings.NAFNET_MAX_MEAN_CHROMA_SHIFT
        or luminance_shift > settings.NAFNET_MAX_MEAN_LUMINANCE_SHIFT
    ):
        raise HDRestorationUnavailable(
            "The NAFNet candidate failed the image-fidelity guard."
        )
    return output, {
        "provider": "nafnet_gopro_width64",
        "checkpoint_sha256_prefix": settings.NAFNET_MODEL_SHA256[:12],
        "chroma_shift": round(chroma_shift, 3),
        "luminance_shift": round(luminance_shift, 3),
        **inference,
    }


def denoise_with_nafnet(image):
    """Real-noise denoising with NAFNet-SIDD-width64, held to the same fidelity limits as the deblurring model."""
    if image is None or image.size == 0:
        raise HDRestorationUnavailable("The NAFNet-SIDD input image is invalid.")
    output, inference = _infer("nafnet_sidd", "NAFNet-SIDD", image, 1)
    # Removing noise legitimately changes every pixel a little, so the fidelity limits are applied to the
    # low-frequency colour and brightness (both sides softened), where a real drift would show.
    source_lab = cv2.cvtColor(cv2.GaussianBlur(image, (0, 0), 2.0), cv2.COLOR_BGR2LAB).astype(np.float32)
    output_lab = cv2.cvtColor(cv2.GaussianBlur(output, (0, 0), 2.0), cv2.COLOR_BGR2LAB).astype(np.float32)
    chroma_shift = float(np.mean(np.abs(output_lab[:, :, 1:] - source_lab[:, :, 1:])))
    luminance_shift = float(np.mean(np.abs(output_lab[:, :, 0] - source_lab[:, :, 0])))
    if (
        chroma_shift > settings.NAFNET_MAX_MEAN_CHROMA_SHIFT
        or luminance_shift > settings.NAFNET_MAX_MEAN_LUMINANCE_SHIFT
    ):
        raise HDRestorationUnavailable("The NAFNet-SIDD candidate failed the image-fidelity guard.")
    return output, {
        "provider": "nafnet_sidd_width64",
        "checkpoint_sha256_prefix": settings.NAFNET_SIDD_MODEL_SHA256[:12],
        "chroma_shift": round(chroma_shift, 3),
        "luminance_shift": round(luminance_shift, 3),
        **inference,
    }


def upscale_with_realesrgan(image):
    if image is None or image.size == 0:
        raise HDRestorationUnavailable("The Real-ESRGAN input image is invalid.")
    height, width = image.shape[:2]
    scale = int(settings.HD_RESTORATION_OUTPUT_SCALE)
    if scale not in (2, 4):
        raise HDRestorationUnavailable("HD_RESTORATION_OUTPUT_SCALE must be 2 or 4.")
    # The model is 4x, but tiles are reduced to the output scale before they are
    # accumulated, so memory follows the pixels that are kept. The native limit
    # still applies when the native 4x result is itself the output.
    output_pixels = height * width * scale * scale
    if output_pixels > settings.HD_RESTORATION_MAX_OUTPUT_PIXELS:
        raise HDRestorationUnavailable("The HD output exceeds the configured safety limit.")
    if scale == 4 and output_pixels > settings.HD_RESTORATION_MAX_NATIVE_OUTPUT_PIXELS:
        raise HDRestorationUnavailable("The native 4x output exceeds the configured safety limit.")
    output, inference = _infer("realesrgan", "Real-ESRGAN", image, 4, output_scale=scale)
    return output, {
        "provider": "realesrgan_x4plus",
        "native_scale": 4,
        "output_scale": scale,
        "checkpoint_sha256_prefix": settings.REALESRGAN_MODEL_SHA256[:12],
        **inference,
    }

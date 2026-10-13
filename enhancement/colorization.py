"""Checksum-pinned local SIGGRAPH17 automatic colorization adapter.

Reimplements richzhang/colorization's SIGGRAPHGenerator (BSD-2-Clause,
https://github.com/richzhang/colorization) with no hint channels, i.e. fully
automatic colorization. The network only ever predicts the ab (colour)
channels; the original L (luminance) channel is passed through untouched, so
structure and exposure are always preserved exactly. This is intentionally
run as the final pipeline stage (see ``colorization_stage`` in
``services.py``): it must not interact with any earlier chroma-preservation
or chroma-shift guard, which are written to keep near-original hue and would
otherwise fight a deliberate near-zero-to-full-colour change.
"""

import hashlib
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings

from . import runtime


class ColorizationUnavailable(RuntimeError):
    """Raised when the colorization model cannot run safely."""


# Shared with every other adapter: one heavy inference at a time per process.
_MODEL_LOCK = runtime.INFERENCE_LOCK
_MODEL_CACHE = {}

_PAD_MULTIPLE = 8
_L_CENT = 50.0
_L_NORM = 100.0
_AB_NORM = 110.0


def _runtime():
    try:
        import torch
        from torch import nn
        from torch.nn import functional as functional
    except ImportError as error:
        raise ColorizationUnavailable("The isolated PyTorch runtime is unavailable.") from error
    return torch, nn, functional


def _verified_path(path_value, expected_hash, label):
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ColorizationUnavailable(f"The {label} checkpoint is unavailable.")
    expected = str(expected_hash).strip().lower()
    if len(expected) != 64:
        raise ColorizationUnavailable(f"The {label} checksum is not configured.")
    hasher = hashlib.sha256()
    with path.open("rb") as checkpoint:
        for chunk in iter(lambda: checkpoint.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise ColorizationUnavailable(f"The {label} checkpoint failed integrity verification.")
    return path


def _device(torch):
    return runtime.resolve_device(torch, settings.COLORIZATION_DEVICE, "colorization", ColorizationUnavailable)


def _build_siggraph17(nn, functional):
    torch, _nn, _functional = _runtime()
    classes = 529

    def block(*layers):
        return nn.Sequential(*layers)

    class SIGGRAPHGenerator(nn.Module):
        def __init__(self):
            super().__init__()
            norm_layer = nn.BatchNorm2d
            self.model1 = block(
                nn.Conv2d(4, 64, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(64, 64, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(64),
            )
            self.model2 = block(
                nn.Conv2d(64, 128, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(128, 128, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(128),
            )
            self.model3 = block(
                nn.Conv2d(128, 256, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(256, 256, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(256, 256, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(256),
            )
            self.model4 = block(
                nn.Conv2d(256, 512, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(512),
            )
            self.model5 = block(
                nn.Conv2d(512, 512, 3, dilation=2, stride=1, padding=2, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, dilation=2, stride=1, padding=2, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, dilation=2, stride=1, padding=2, bias=True), nn.ReLU(True), norm_layer(512),
            )
            self.model6 = block(
                nn.Conv2d(512, 512, 3, dilation=2, stride=1, padding=2, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, dilation=2, stride=1, padding=2, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, dilation=2, stride=1, padding=2, bias=True), nn.ReLU(True), norm_layer(512),
            )
            self.model7 = block(
                nn.Conv2d(512, 512, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(512, 512, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(512),
            )
            self.model8up = block(nn.ConvTranspose2d(512, 256, 4, 2, 1, bias=True))
            self.model3short8 = block(nn.Conv2d(256, 256, 3, 1, 1, bias=True))
            self.model8 = block(
                nn.ReLU(True), nn.Conv2d(256, 256, 3, 1, 1, bias=True), nn.ReLU(True),
                nn.Conv2d(256, 256, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(256),
            )
            self.model9up = block(nn.ConvTranspose2d(256, 128, 4, 2, 1, bias=True))
            self.model2short9 = block(nn.Conv2d(128, 128, 3, 1, 1, bias=True))
            self.model9 = block(
                nn.ReLU(True), nn.Conv2d(128, 128, 3, 1, 1, bias=True), nn.ReLU(True), norm_layer(128),
            )
            self.model10up = block(nn.ConvTranspose2d(128, 128, 4, 2, 1, bias=True))
            self.model1short10 = block(nn.Conv2d(64, 128, 3, 1, 1, bias=True))
            self.model10 = block(
                nn.ReLU(True), nn.Conv2d(128, 128, 3, dilation=1, stride=1, padding=1, bias=True),
                nn.LeakyReLU(negative_slope=0.2),
            )
            self.model_class = block(nn.Conv2d(256, classes, 1, padding=0, dilation=1, stride=1, bias=True))
            self.model_out = block(nn.Conv2d(128, 2, 1, padding=0, dilation=1, stride=1, bias=True), nn.Tanh())
            self.upsample4 = block(nn.Upsample(scale_factor=4, mode="bilinear"))
            self.softmax = block(nn.Softmax(dim=1))

        def forward(self, input_l):
            zeros = input_l * 0
            hint_ab = torch.cat((zeros, zeros), dim=1)
            mask = zeros
            normalized_l = (input_l - _L_CENT) / _L_NORM
            normalized_ab = hint_ab / _AB_NORM

            conv1_2 = self.model1(torch.cat((normalized_l, normalized_ab, mask), dim=1))
            conv2_2 = self.model2(conv1_2[:, :, ::2, ::2])
            conv3_3 = self.model3(conv2_2[:, :, ::2, ::2])
            conv4_3 = self.model4(conv3_3[:, :, ::2, ::2])
            conv5_3 = self.model5(conv4_3)
            conv6_3 = self.model6(conv5_3)
            conv7_3 = self.model7(conv6_3)

            conv8_up = self.model8up(conv7_3) + self.model3short8(conv3_3)
            conv8_3 = self.model8(conv8_up)
            conv9_up = self.model9up(conv8_3) + self.model2short9(conv2_2)
            conv9_3 = self.model9(conv9_up)
            conv10_up = self.model10up(conv9_3) + self.model1short10(conv1_2)
            conv10_2 = self.model10(conv10_up)
            out_reg = self.model_out(conv10_2)
            return out_reg * _AB_NORM

    return SIGGRAPHGenerator()


def _load_model():
    path = _verified_path(settings.COLORIZATION_MODEL_PATH, settings.COLORIZATION_MODEL_SHA256, "colorization")
    torch, nn, functional = _runtime()
    device = _device(torch)
    key = (str(path), str(device))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], device
    model = _build_siggraph17(nn, functional)
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        state = checkpoint.get("state_dict", checkpoint.get("params_ema", checkpoint.get("params", checkpoint)))
        model.load_state_dict(state, strict=True)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as error:
        raise ColorizationUnavailable("The colorization checkpoint is incompatible.") from error
    model.eval().to(device)
    _MODEL_CACHE[key] = model
    return model, device


def _predict_ab(l_channel, model, device):
    """Run the network on a padded L channel and crop ab back to size."""
    torch, _nn, _functional = _runtime()
    height, width = l_channel.shape
    pad_h = (-height) % _PAD_MULTIPLE
    pad_w = (-width) % _PAD_MULTIPLE
    padded = np.pad(l_channel, ((0, pad_h), (0, pad_w)), mode="edge")
    tensor = torch.from_numpy(padded[None, None, :, :].astype(np.float32)).to(device)
    with torch.inference_mode():
        ab = model(tensor)
    ab = ab.squeeze(0).cpu().numpy().transpose(1, 2, 0)
    return ab[:height, :width, :]


def colorize_with_siggraph17(image):
    """Colorize a BGR image, preserving its original luminance exactly."""
    if image is None or image.size == 0:
        raise ColorizationUnavailable("The colorization input image is invalid.")
    height, width = image.shape[:2]
    if height * width > settings.COLORIZATION_MAX_INPUT_PIXELS:
        raise ColorizationUnavailable("The requested colorization input exceeds the safety limit.")

    rgb_float = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    lab_full = cv2.cvtColor(rgb_float, cv2.COLOR_RGB2LAB)
    full_l = lab_full[:, :, 0]

    max_edge = settings.COLORIZATION_MAX_INFERENCE_EDGE
    longest_edge = max(height, width)
    if longest_edge > max_edge:
        scale = max_edge / float(longest_edge)
        working_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        working_rgb = cv2.resize(rgb_float, working_size, interpolation=cv2.INTER_AREA)
        working_l = cv2.cvtColor(working_rgb, cv2.COLOR_RGB2LAB)[:, :, 0]
    else:
        working_l = full_l

    with _MODEL_LOCK:
        model, device = _load_model()
        try:
            working_ab = _predict_ab(working_l, model, device)
        except (RuntimeError, MemoryError) as error:
            # One bounded forward pass (COLORIZATION_MAX_INFERENCE_EDGE), so there
            # is no smaller tile to retry with: keep the uncoloured image.
            runtime.release_memory(device)
            reason = "ran out of memory" if runtime.is_out_of_memory(error) else "failed safely"
            raise ColorizationUnavailable(f"Colorization inference {reason}.") from error

    if working_ab.shape[:2] != full_l.shape:
        full_ab = cv2.resize(working_ab, (width, height), interpolation=cv2.INTER_LINEAR)
    else:
        full_ab = working_ab

    colorized_lab = np.dstack((full_l, full_ab)).astype(np.float32)
    colorized_rgb = np.clip(cv2.cvtColor(colorized_lab, cv2.COLOR_LAB2RGB), 0.0, 1.0)
    colorized_bgr = cv2.cvtColor(np.rint(colorized_rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    if colorized_bgr.shape != image.shape:
        raise ColorizationUnavailable("The colorized candidate did not preserve the source pixel grid.")

    # Lab->RGB gamut clipping can distort perceived luminance even though the
    # true-Lab L channel we fed in was untouched (out-of-gamut ab predictions
    # get clamped in RGB space). Worse, the rest of this codebase measures
    # "luminance" as cv2.COLOR_BGR2GRAY's ITU-R BT.601 luma, which is a
    # *different* quantity from Lab's L* and can diverge from it noticeably
    # for saturated colours. Re-impose the exact original BT.601 luma via the
    # YCrCb Y channel (mirroring _preserve_source_chroma's channel-swap
    # technique) so downstream quality metrics see byte-identical luminance,
    # then verify the residual drift with the same kind of fidelity guard
    # NAFNet/PureVision use before accepting the candidate.
    source_ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
    candidate_ycrcb = cv2.cvtColor(colorized_bgr, cv2.COLOR_BGR2YCrCb)
    candidate_ycrcb[:, :, 0] = source_ycrcb[:, :, 0]
    colorized_bgr = cv2.cvtColor(candidate_ycrcb, cv2.COLOR_YCrCb2BGR)

    source_luminance = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    candidate_luminance = cv2.cvtColor(colorized_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    luminance_shift = float(np.mean(np.abs(candidate_luminance - source_luminance)))
    if luminance_shift > settings.COLORIZATION_MAX_MEAN_LUMINANCE_SHIFT:
        raise ColorizationUnavailable("The colorization candidate failed the luminance-fidelity guard.")

    return colorized_bgr, {
        "provider": "colorization_siggraph17",
        "checkpoint_sha256_prefix": settings.COLORIZATION_MODEL_SHA256[:12],
        "license": "BSD-2-Clause",
        "inference_edge": min(longest_edge, max_edge),
        "device": str(device),
    }

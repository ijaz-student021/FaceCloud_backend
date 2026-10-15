"""The local tone renderer: bounded controls applied in CIE LAB (OpenCV + NumPy).

Semantics of each control are in ``controls.py``. Properties this module
guarantees, each covered by a test:

* Neutral controls return the source pixel for pixel. (The previous renderer
  converted through 8-bit LAB, which alone changed 38 % of the pixels of real
  photos by up to 20 levels, even for an untouched photo.)
* Work is done in float LAB and rounded once at the end, so an untouched
  channel is not re-quantised.
* Every control is a single pass: no stacked or repeated exposure, contrast or
  sharpening.
* Global controls commute with downscaling, and sharpness uses a radius
  proportional to the image's longer edge, so rendering a small preview and
  rendering the full-resolution image then shrinking it give the same look.
"""
import cv2
import numpy as np

from .controls import CONTROL_NAMES, is_neutral

# L in OpenCV's float LAB is 0-100; the control semantics are written on the
# 8-bit scale (0-255) and a/b centred on 128, which these constants convert.
_L_SCALE = 255.0 / 100.0
_AB_CENTRE = 128.0

# Unsharp radius: sigma = SHARPNESS_SIGMA_PER_EDGE * longer_edge / 1024, floor 0.5 px.
SHARPNESS_SIGMA_AT_1024 = 0.68  # the standard deviation of Pillow's SMOOTH kernel
SHARPNESS_MIN_SIGMA = 0.5


def sharpness_sigma(shape):
    return max(SHARPNESS_MIN_SIGMA, SHARPNESS_SIGMA_AT_1024 * max(shape[:2]) / 1024.0)


def render_tone(source, controls, strength=1.0):
    """Return ``(rendered_bgr_uint8, effective)`` for 8-bit BGR ``source``.

    ``strength`` (0-1) scales every control, which is how the safety guards
    reduce a render. ``effective`` holds the values actually applied.
    """
    effective = {name: float(controls.get(name, 0.0)) * strength for name in CONTROL_NAMES}
    if is_neutral(effective):
        return source.copy(), effective

    lab = cv2.cvtColor(source.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    lightness = lab[:, :, 0] * _L_SCALE
    channel_a = lab[:, :, 1] + _AB_CENTRE
    channel_b = lab[:, :, 2] + _AB_CENTRE

    lightness = lightness + effective["brightness"] * 0.35
    midpoint = float(np.median(lightness))
    lightness = midpoint + (lightness - midpoint) * (1.0 + effective["contrast"] * 0.005)

    normalized = np.clip(lightness / 255.0, 0.0, 1.0)
    lightness = (
        lightness
        + np.square(1.0 - normalized) * effective["shadows"] * 0.45
        + np.square(normalized) * effective["highlights"] * 0.40
    )

    saturation = 1.0 + effective["saturation"] * 0.004
    channel_a = _AB_CENTRE + (channel_a - _AB_CENTRE) * saturation
    channel_b = _AB_CENTRE + (channel_b - _AB_CENTRE) * saturation
    channel_b = channel_b + effective["warmth"] * 0.30

    if effective["sharpness"]:
        blurred = cv2.GaussianBlur(lightness, (0, 0), sharpness_sigma(source.shape))
        lightness = lightness + (effective["sharpness"] / 100.0) * (lightness - blurred)

    lab[:, :, 0] = np.clip(lightness, 0.0, 255.0) / _L_SCALE
    lab[:, :, 1] = np.clip(channel_a, 0.0, 255.0) - _AB_CENTRE
    lab[:, :, 2] = np.clip(channel_b, 0.0, 255.0) - _AB_CENTRE
    rendered = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return np.clip(np.rint(rendered * 255.0), 0, 255).astype(np.uint8), effective

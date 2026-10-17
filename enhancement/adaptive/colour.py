"""Colour handling for a low-light lift: keep the photograph's own colours, take only the brightness from the network.

Why this exists (measured, docs/evaluation/2026-10-15-lowlight-colour.md): Retinexformer returns a brighter image whose colours
are paler than the scene's (chroma retention against the clean photograph 0.48-0.81, hue shifted 6-10 degrees), and the earlier
fix, the source's CIELAB chroma scaled by the square root of the lightness gain, still left the result 20-40 % paler on dark
inputs. CIELAB chroma grows roughly in proportion to lightness under an exposure change, so a square root under-restores it, and
a larger fixed exponent over-saturates. ``chromaticity_linear`` avoids choosing an exponent: it works in linear light, where
brightening a scene multiplies all three channels by the same factor and therefore leaves their ratios (the chromaticity)
unchanged. The ratios are taken from the (smoothed) source, the luminance from the network.

No colour is invented: the only colour information used is the source's own. The smoothing is what keeps the sensor's colour noise
out of near-black pixels, as the earlier method did.
"""
import cv2
import numpy as np

SMOOTHING_SIGMA = 3.5
WORKING_EDGE = 512


def to_linear(image_bgr):
    c = image_bgr.astype(np.float32) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def from_linear(linear):
    c = np.clip(linear, 0.0, 1.0)
    encoded = np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)
    return np.clip(np.rint(encoded * 255.0), 0, 255).astype(np.uint8)


def luminance_linear(linear_bgr):
    return 0.0722 * linear_bgr[..., 0] + 0.7152 * linear_bgr[..., 1] + 0.2126 * linear_bgr[..., 2]


def chromaticity_linear(source, raw):
    """``source`` and ``raw`` are BGR uint8 of the same size; returns BGR uint8 with the source's chromaticity and ``raw``'s luminance.

    A pixel whose recombined linear value would leave the display range is scaled down uniformly (hue and saturation kept), so
    bright saturated areas are not clipped channel by channel into a different colour.
    """
    height, width = source.shape[:2]
    scale = min(1.0, WORKING_EDGE / max(height, width))
    small = (max(1, round(width * scale)), max(1, round(height * scale)))
    shrunk = cv2.GaussianBlur(cv2.resize(to_linear(source), small, interpolation=cv2.INTER_AREA), (0, 0), SMOOTHING_SIGMA)
    luminance = np.maximum(luminance_linear(shrunk), 1e-5)[..., None]
    ratios = cv2.resize(shrunk / luminance, (width, height), interpolation=cv2.INTER_LINEAR)
    ratios = ratios / np.maximum(luminance_linear(ratios), 1e-5)[..., None]
    out = ratios * luminance_linear(to_linear(raw))[..., None]
    return from_linear(out / np.maximum(out.max(axis=2, keepdims=True), 1.0))


def _smooth_chromaticity(image, size):
    """Luminance-normalised linear colour (r, g, b over luminance, minus the neutral 1) of the smoothed image, and its luminance."""
    linear = cv2.GaussianBlur(cv2.resize(to_linear(image), size, interpolation=cv2.INTER_AREA), (0, 0), 2.0)
    luminance = np.maximum(luminance_linear(linear), 1e-6)
    return linear / luminance[..., None] - 1.0, luminance


def lift_colour_metrics(parent, candidate, min_luminance=0.002):
    """How well a brightened candidate keeps its parent's colour, without a clean reference.

    Exposure changes leave the chromaticity of linear light (colour ratios) unchanged, so that is what is compared, on pixels
    that were bright enough in the parent to carry a measurable colour (smoothed linear luminance above ``min_luminance``) and
    coloured enough to have a direction:

    ``hue_shift``: mean angle (degrees) between the parent's and the candidate's colour direction.
    ``saturation_ratio``: median of the candidate's distance from neutral over the parent's: 1.0 keeps the colourfulness, clearly
    below 1 is the pale, grey look, clearly above 1 is over-saturation.

    Both are None when the photograph has too little colour to judge (a grey scene). CIELAB chroma/lightness is deliberately not
    used: it is not invariant under an exposure change (the CIELAB toe is linear), so a perfect lift would not read as one.
    """
    if candidate.shape[:2] != parent.shape[:2]:
        candidate = cv2.resize(candidate, (parent.shape[1], parent.shape[0]), interpolation=cv2.INTER_AREA)
    scale = min(1.0, WORKING_EDGE / max(parent.shape[:2]))
    size = (max(1, round(parent.shape[1] * scale)), max(1, round(parent.shape[0] * scale)))
    p, parent_luminance = _smooth_chromaticity(parent, size)
    c, _ = _smooth_chromaticity(candidate, size)
    length_p, length_c = np.linalg.norm(p, axis=2), np.linalg.norm(c, axis=2)
    usable = (parent_luminance > min_luminance) & (length_p > 0.15) & (length_c > 0.02)
    if usable.sum() < 200:
        return {"hue_shift": None, "saturation_ratio": None}
    cosine = np.clip((p * c).sum(axis=2)[usable] / (length_p[usable] * length_c[usable]), -1.0, 1.0)
    return {"hue_shift": float(np.degrees(np.arccos(cosine)).mean()), "saturation_ratio": float(np.median(length_c[usable] / length_p[usable]))}

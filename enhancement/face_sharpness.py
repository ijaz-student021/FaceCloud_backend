"""How sharp the face itself is, independent of the background.

The whole-frame Laplacian variance mixes the subject with everything around it: a portrait-mode photo (sharp face, blurred background) reads
as blurred, and a blurred subject in front of a sharp background reads as sharp. The face region is measured on its own, resized to a fixed
128 px so the number does not depend on how large the face is in the frame, and with an inset so hair and background edges at the border
of the detector's box do not count.

Measured on 12 portraits (``enhancement/adaptive/analyzer.py`` calibration): a sharp face about 119 (10th percentile 81), a Gaussian blur of
1.5 px about 21, of 3 px about 7, a face from a 2.5x smaller (low-resolution) photo about 36, JPEG quality 15 about 90.
"""
import cv2
import numpy as np

FACE_DETAIL_SIZE = 128
MIN_REGION_EDGE = 16


def face_region(image, box, inset=0.15):
    """The part of ``image`` inside ``box`` (x, y, width, height), shrunk by ``inset`` on every side; ``None`` if nothing is left."""
    height, width = image.shape[:2]
    x, y, w, h = box
    pad_x, pad_y = int(inset * w), int(inset * h)
    x0, y0 = max(0, int(x) + pad_x), max(0, int(y) + pad_y)
    x1, y1 = min(width, int(x + w) - pad_x), min(height, int(y + h) - pad_y)
    return image[y0:y1, x0:x1] if x1 > x0 and y1 > y0 else None


def face_detail(region_luminance):
    """Laplacian variance of a face region's luminance resized to 128 px (higher = more detail); ``None`` when the region is too small."""
    if region_luminance is None or region_luminance.size == 0 or min(region_luminance.shape[:2]) < MIN_REGION_EDGE:
        return None
    standard = cv2.resize(region_luminance.astype(np.float32), (FACE_DETAIL_SIZE, FACE_DETAIL_SIZE), interpolation=cv2.INTER_AREA)
    return round(float(cv2.Laplacian(standard, cv2.CV_32F).var()), 1)


def luminance_of(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def primary_face_detail(image, box, inset=0.15):
    """The detail measure for the face in ``box`` of a BGR (or grey) ``image``."""
    region = face_region(image, box, inset)
    return None if region is None else face_detail(luminance_of(region))

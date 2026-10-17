"""The editor's stable source: one enhanced result, identified by a version, decoded with its alpha.

* ``source_version`` names the exact enhanced image an edit or a preview was
  made from. An enhanced result is immutable once completed, so the version
  changes only if the result or its file does; a save or preview that names a
  different version than the current one is refused instead of silently
  applying to something the person never saw.
* The editor always renders from the *enhanced* image, never from an earlier
  edit, so edits do not stack and no edit compresses a previous edit again.
  An edit is one more encode of the enhanced image (itself one encode of the
  pipeline's output); this is documented, not hidden.
* Transparency: a PNG result keeps its alpha channel through every edit and
  preview. A result stored as JPEG has no alpha: when its source had
  transparency it was composited onto PREPROCESS_TRANSPARENT_BACKGROUND (white
  by default) at preprocessing, before enhancement, and edits keep that format.
"""
import hashlib

import cv2
import numpy as np
from django.conf import settings
from rest_framework.exceptions import ValidationError


def source_version(result):
    """A short stable identifier of the enhanced image of ``result``."""
    basis = "|".join(str(part) for part in (
        result.pk, result.finished_at.isoformat() if result.finished_at else "",
        result.output_file_size, result.output_name, result.output_width, result.output_height,
    ))
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def decode_with_alpha(payload):
    """``(bgr uint8, alpha uint8 or None)`` from encoded image bytes."""
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise ValidationError("The enhanced image cannot be decoded safely.")
    if decoded.dtype == np.uint16:
        decoded = (decoded / 257.0).round().astype(np.uint8)
    if decoded.dtype != np.uint8:
        raise ValidationError("The enhanced image cannot be decoded safely.")
    if decoded.ndim == 2:
        return cv2.cvtColor(decoded, cv2.COLOR_GRAY2BGR), None
    if decoded.shape[2] == 4:
        alpha = decoded[:, :, 3].copy()
        bgr = np.ascontiguousarray(decoded[:, :, :3])
        return (bgr, alpha) if int(alpha.min()) < 255 else (bgr, None)
    return np.ascontiguousarray(decoded[:, :, :3]), None


def read_enhanced_with_alpha(result):
    """The result's enhanced file as ``(bgr, alpha)``."""
    if not result.enhanced_file:
        raise ValidationError("The enhanced image is unavailable.")
    result.enhanced_file.open("rb")
    try:
        payload = result.enhanced_file.read()
    finally:
        result.enhanced_file.close()
    return decode_with_alpha(payload)


def visible_for_measurement(bgr, alpha):
    """Colour of fully transparent pixels replaced by the visible average.

    Their stored colour is arbitrary; left alone it would pass for deep shadow or
    blown highlights in the measurements that drive the guards.
    """
    if alpha is None:
        return bgr
    hidden = alpha == 0
    if not hidden.any():
        return bgr
    visible = ~hidden
    colour = np.rint(bgr[visible].mean(axis=0)).astype(np.uint8) if visible.any() else np.full(3, 128, np.uint8)
    prepared = bgr.copy()
    prepared[hidden] = colour
    return prepared


def restore_hidden(rendered, original, alpha):
    """Put the original colour back under fully transparent pixels, so nothing hidden changes."""
    if alpha is None:
        return rendered
    hidden = alpha == 0
    if hidden.any():
        rendered = rendered.copy()
        rendered[hidden] = original[hidden]
    return rendered


def attach(rendered, alpha):
    return np.dstack((rendered, alpha)) if alpha is not None else rendered


def resize_to_edge(image, alpha, long_edge):
    """Shrink (never enlarge) so the longer edge is at most ``long_edge``; alpha follows."""
    height, width = image.shape[:2]
    if max(height, width) <= long_edge:
        return image, alpha
    scale = long_edge / float(max(height, width))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    if alpha is not None:
        alpha = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
    return image, alpha


def transparency_policy(output_format):
    """What the editor does about transparency, stated for clients and recorded on edits."""
    return {
        "png": "alpha preserved through every edit and preview",
        "jpeg": (
            "no alpha: a source with transparency was composited onto "
            f"{settings.PREPROCESS_TRANSPARENT_BACKGROUND} at preprocessing; edits keep the result's format"
        ),
        "this_result": output_format,
    }

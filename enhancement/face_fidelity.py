"""Face-region fidelity check for the enhancement pipeline.

The whole-image guards average over every pixel, so a restoration model can
smooth a face into wax, ring it with sharpening halos or shift its skin tone
while the frame as a whole stays inside its limits. This check measures each
detected face on its own, against the image as it was before any restoration
model or sharpening ran, and blends a face that moved too far back toward that
reference.

It only locates faces (the same detector upload validation uses). It computes
no embedding and decides nothing about who anyone is, and it stores measurements
only, never face coordinates. It can only ever reduce enhancement strength on a
face; it never adds detail.

What it does not prove: passing means a face stayed close to the reference in
structure, texture energy, colour and clipping. It is not evidence that restored
detail is *true* detail, and it does not replace the identity guard.
"""
import logging

import cv2
import numpy as np
from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)

# Strengths tried, in order, for a face that violates a limit: full candidate
# first, then progressively more of the reference, ending with the reference.
_BLEND_STRENGTHS = (1.0, 0.7, 0.4, 0.0)
_BOX_MARGIN = 0.15


def _limits():
    return {
        "minimum_structural_similarity": float(settings.FACE_FIDELITY_MIN_STRUCTURAL_SIMILARITY),
        "minimum_texture_retention": float(settings.FACE_FIDELITY_MIN_TEXTURE_RETENTION),
        "maximum_texture_gain": float(settings.FACE_FIDELITY_MAX_TEXTURE_GAIN),
        "maximum_mean_chroma_shift": float(settings.FACE_FIDELITY_MAX_CHROMA_SHIFT),
        "maximum_highlight_clipping_growth": float(settings.FACE_FIDELITY_MAX_HIGHLIGHT_CLIPPING_GROWTH),
    }


def _texture_energy(gray):
    """Fine-detail energy: what is left after removing everything smooth."""
    gray = gray.astype(np.float32)
    return float(np.std(gray - cv2.GaussianBlur(gray, (0, 0), 2.0)))


def face_metrics(reference, candidate):
    """Compare one face crop with its reference; both on the same pixel grid."""
    from .services import _mean_chroma_shift, _structural_similarity

    reference_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    candidate_gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
    reference_texture = _texture_energy(reference_gray)
    candidate_texture = _texture_energy(candidate_gray)
    clipped_before = float(np.mean(reference_gray >= 250) * 100)
    clipped_after = float(np.mean(candidate_gray >= 250) * 100)
    return {
        "structural_similarity": round(_structural_similarity(reference, candidate), 4),
        # Below 1: detail was smoothed away (waxy skin). Above 1: detail energy
        # was added (over-sharpening, halos, amplified noise, invented texture).
        "texture_ratio": round(candidate_texture / max(reference_texture, 0.5), 3),
        "mean_chroma_shift": round(_mean_chroma_shift(reference, candidate), 3),
        "highlight_clipping_growth": round(clipped_after - clipped_before, 3),
    }


def _violations(metrics, limits):
    found = []
    if metrics["structural_similarity"] < limits["minimum_structural_similarity"]:
        found.append("structural_similarity")
    if metrics["texture_ratio"] < limits["minimum_texture_retention"]:
        found.append("texture_loss")
    if metrics["texture_ratio"] > limits["maximum_texture_gain"]:
        found.append("texture_gain")
    if metrics["mean_chroma_shift"] > limits["maximum_mean_chroma_shift"]:
        found.append("chroma_shift")
    if metrics["highlight_clipping_growth"] > limits["maximum_highlight_clipping_growth"]:
        found.append("highlight_clipping")
    return found


def _detect_boxes(image):
    """Largest face boxes in ``image``, as fractions of its width and height."""
    from validation.services import get_face_detector

    height, width = image.shape[:2]
    longest = max(height, width)
    max_edge = int(settings.FACE_FIDELITY_MAX_EDGE)
    working = image
    if longest > max_edge:
        scale = max_edge / float(longest)
        working = cv2.resize(
            image, (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    working_height, working_width = working.shape[:2]
    detection = get_face_detector().detect(working)
    minimum_edge = int(settings.FACE_FIDELITY_MIN_FACE_EDGE)
    boxes = [
        box for box in detection.boxes
        if min(box[2], box[3]) >= minimum_edge and box[2] > 0 and box[3] > 0
    ]
    boxes.sort(key=lambda box: box[2] * box[3], reverse=True)
    return [
        (x / working_width, y / working_height, w / working_width, h / working_height)
        for x, y, w, h in boxes[: int(settings.FACE_FIDELITY_MAX_FACES)]
    ]


def _pixel_box(fraction_box, width, height):
    x, y, w, h = fraction_box
    left = int(max(0, (x - w * _BOX_MARGIN) * width))
    top = int(max(0, (y - h * _BOX_MARGIN) * height))
    right = int(min(width, (x + w * (1 + _BOX_MARGIN)) * width))
    bottom = int(min(height, (y + h * (1 + _BOX_MARGIN)) * height))
    return left, top, right, bottom


def _feathered_mask(height, width):
    """An ellipse that is 1 over the face and fades to 0 at the edge of the box."""
    mask = np.zeros((height, width), dtype=np.float32)
    cv2.ellipse(
        mask, (width // 2, height // 2), (max(1, int(width * 0.42)), max(1, int(height * 0.46))),
        0, 0, 360, 1.0, -1,
    )
    sigma = max(1.0, min(height, width) * 0.06)
    return cv2.GaussianBlur(mask, (0, 0), sigma)[:, :, None]


def check_face_regions(reference, candidate):
    """Return ``(image, metadata)`` with every detected face inside its limits.

    ``reference`` is the last image accepted before restoration and sharpening;
    ``candidate`` is the image after them, possibly on a larger pixel grid. Each
    face is measured on the reference's grid, so an upscaler is judged on what it
    changed, not on having more pixels.
    """
    limits = _limits()
    metadata = {
        "enabled": True,
        "status": "no_faces",
        "faces_checked": 0,
        "faces_adjusted": 0,
        "faces": [],
        "limits": limits,
        "fallback_reason": "",
    }
    try:
        boxes = _detect_boxes(reference)
    except (ValidationError, cv2.error) as error:
        metadata["status"] = "unavailable"
        metadata["fallback_reason"] = "The face detector is unavailable."
        logger.warning("Face-region check skipped: %s", error)
        return candidate, metadata
    if not boxes:
        return candidate, metadata

    reference_height, reference_width = reference.shape[:2]
    candidate_height, candidate_width = candidate.shape[:2]
    same_grid = (reference_height, reference_width) == (candidate_height, candidate_width)
    output = candidate
    for fraction_box in boxes:
        left, top, right, bottom = _pixel_box(fraction_box, reference_width, reference_height)
        if right - left < 8 or bottom - top < 8:
            continue
        reference_face = reference[top:bottom, left:right]
        if same_grid:
            out_left, out_top, out_right, out_bottom = left, top, right, bottom
        else:
            out_left, out_top, out_right, out_bottom = _pixel_box(
                fraction_box, candidate_width, candidate_height,
            )
        candidate_face = output[out_top:out_bottom, out_left:out_right]
        face_height, face_width = candidate_face.shape[:2]
        # The reference on the candidate's grid (for blending) and the candidate
        # on the reference's grid (for measuring).
        reference_on_output = reference_face if same_grid else cv2.resize(
            reference_face, (face_width, face_height), interpolation=cv2.INTER_CUBIC,
        )
        mask = None
        record = {"attempts": []}
        accepted_face = candidate_face
        for strength in _BLEND_STRENGTHS:
            if strength >= 1.0:
                trial = candidate_face
            else:
                if mask is None:
                    mask = _feathered_mask(face_height, face_width)
                weight = mask * (1.0 - strength)
                trial = np.clip(
                    candidate_face.astype(np.float32) * (1.0 - weight)
                    + reference_on_output.astype(np.float32) * weight,
                    0, 255,
                ).astype(np.uint8)
            comparable = trial if same_grid else cv2.resize(
                trial, (right - left, bottom - top), interpolation=cv2.INTER_AREA,
            )
            metrics = face_metrics(reference_face, comparable)
            violations = _violations(metrics, limits)
            record["attempts"].append(
                {"strength": strength, "metrics": metrics, "violations": violations}
            )
            accepted_face = trial
            if not violations:
                break
        record["applied_strength"] = record["attempts"][-1]["strength"]
        record["violations_at_full_strength"] = record["attempts"][0]["violations"]
        record["metrics"] = record["attempts"][-1]["metrics"]
        metadata["faces"].append(record)
        metadata["faces_checked"] += 1
        if record["applied_strength"] < 1.0:
            metadata["faces_adjusted"] += 1
            if output is candidate:
                output = candidate.copy()
            output[out_top:out_bottom, out_left:out_right] = accepted_face

    if not metadata["faces_checked"]:
        return candidate, metadata
    if not metadata["faces_adjusted"]:
        metadata["status"] = "passed"
    elif all(face["applied_strength"] == 0.0 for face in metadata["faces"] if face["applied_strength"] < 1.0):
        metadata["status"] = "reverted"
    else:
        metadata["status"] = "strength_reduced"
    return output, metadata

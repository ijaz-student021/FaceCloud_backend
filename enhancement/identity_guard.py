"""Identity-preserving enhancement guard.

Enhancement models can quietly change how a person looks. This guard measures
that with the same YuNet detector and SFace embeddings the identity-grouping
feature already uses: faces in the source are matched to faces in the
enhanced image and their embedding similarity is checked. If any face drifts
below the minimum, the result is blended back toward the source along a
strength ladder (cheap, and needs no model to run again) until identity is
preserved.

The guard never invents pixels and never runs when it cannot judge: with no
reliable face, or with the detector/identity model unavailable, the enhanced
image is returned unchanged with a recorded reason. The identity model is only
loaded when a face is actually found.
"""

import cv2
from django.conf import settings
from rest_framework.exceptions import ValidationError

_MIN_MATCH_IOU = 0.3


class IdentityGuardUnavailable(RuntimeError):
    """Raised when the detector or identity model cannot be used safely."""


def _load_detector():
    from validation.services import get_face_detector

    try:
        return get_face_detector()
    except ValidationError as error:
        raise IdentityGuardUnavailable("The face detector is unavailable.") from error


def _load_provider():
    from identities.providers import get_identity_provider

    try:
        provider = get_identity_provider()
        provider.healthcheck()
    except ValidationError as error:
        raise IdentityGuardUnavailable("The identity model is unavailable.") from error
    return provider


def _fit_to_edge(image, max_edge):
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _iou(first, second):
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _detect_faces(image, detector, min_edge):
    """Largest faces that are big enough to judge and have alignment landmarks."""
    detection = detector.detect(image)
    candidates = []
    for box, landmarks in zip(detection.boxes, detection.landmarks):
        _x, _y, width, height = box
        if min(width, height) < min_edge or not landmarks or len(landmarks) != 5:
            continue
        candidates.append((width * height, box, landmarks))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(box, landmarks) for _area, box, landmarks in candidates[: settings.IDENTITY_GUARD_MAX_FACES]]


def _embed_faces(image, faces, provider):
    records = []
    for box, landmarks in faces:
        x, y, width, height = box
        try:
            aligned = provider.prepare_face(
                image, {"x": x, "y": y, "width": width, "height": height}, landmarks
            )
            embedding = provider.embed(aligned)
        except ValidationError:
            continue
        records.append({"box": (x, y, width, height), "embedding": embedding})
    return records


def _face_luminance(image, box):
    x, y, width, height = box
    x, y = max(0, x), max(0, y)
    crop = image[y:y + height, x:x + width]
    if crop.size == 0:
        return 0.0
    return float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).mean())


def _worst_face_similarity(source_faces, trial, detector, provider):
    """Lowest source-to-enhanced similarity across the faces found again.

    Returns ``None`` when no source face could be re-detected in ``trial``:
    on grainy, brightened frames the detector routinely misses a face that is
    visibly there, so a missing face is inconclusive rather than proof that
    identity changed.
    """
    lenient_edge = max(1, settings.IDENTITY_GUARD_MIN_FACE_EDGE // 2)
    trial_faces = _embed_faces(trial, _detect_faces(trial, detector, lenient_edge), provider)
    similarities = []
    for source_face in source_faces:
        best_match = max(
            trial_faces,
            key=lambda face: _iou(source_face["box"], face["box"]),
            default=None,
        )
        if best_match is None or _iou(source_face["box"], best_match["box"]) < _MIN_MATCH_IOU:
            continue
        similarities.append(provider.similarity(source_face["embedding"], best_match["embedding"]))
    return min(similarities) if similarities else None


# Every outcome of the guard, reduced to what a reader needs to know about the
# saved image: was identity checked against the source, and did it hold?
IDENTITY_VERIFIED = "verified"          # compared with the source and preserved
IDENTITY_INCONCLUSIVE = "inconclusive"  # no reliable comparison was possible
IDENTITY_UNAVAILABLE = "unavailable"    # no consent, no model, or switched off
IDENTITY_FAILED = "failed"              # compared, and still below the minimum

_STATUS_TO_IDENTITY = {
    "preserved": IDENTITY_VERIFIED,
    "strength_reduced": IDENTITY_VERIFIED,
    "inconclusive": IDENTITY_INCONCLUSIVE,
    "unresolved": IDENTITY_FAILED,
    "disabled": IDENTITY_UNAVAILABLE,
}


def identity_status(metadata):
    """Map the guard's detailed status onto verified/inconclusive/unavailable/failed."""
    status = metadata.get("status")
    if status in _STATUS_TO_IDENTITY:
        return _STATUS_TO_IDENTITY[status]
    # "skipped": either nothing could be compared (no face, face too dark) or
    # the check could not run at all (consent, detector, identity model).
    return IDENTITY_INCONCLUSIVE if metadata.get("reference_checked") else IDENTITY_UNAVAILABLE


class IdentityReference:
    """The source's faces, embedded once and reused for every comparison."""

    def __init__(self, records, dimensions, detector, provider):
        self.records = records
        self.width, self.height = dimensions
        self.detector = detector
        self.provider = provider


def build_identity_reference(source):
    """Embed the source's faces: ``(reference or None, reason)``.

    ``None`` with a reason means the source offers nothing reliable to compare
    against (no face large enough, a face too dark, or one that cannot be
    embedded). Raises ``IdentityGuardUnavailable`` when the detector or identity
    model is missing.
    """
    detector = _load_detector()
    working_source = _fit_to_edge(source, settings.IDENTITY_GUARD_MAX_EDGE)
    source_faces = _detect_faces(
        working_source, detector, settings.IDENTITY_GUARD_MIN_FACE_EDGE
    )
    if not source_faces:
        return None, "No face large enough to judge was found in the source."
    source_faces = [
        (box, landmarks)
        for box, landmarks in source_faces
        if _face_luminance(working_source, box) >= settings.IDENTITY_GUARD_MIN_FACE_LUMINANCE
    ]
    if not source_faces:
        return None, "The source face is too dark for a reliable identity comparison."
    provider = _load_provider()
    source_records = _embed_faces(working_source, source_faces, provider)
    if not source_records:
        return None, "The source faces could not be embedded safely."
    height, width = working_source.shape[:2]
    return IdentityReference(source_records, (width, height), detector, provider), ""


def apply_identity_guard(source, candidate, reference=None):
    """Return ``(image, metadata)`` with identity preserved against ``source``.

    ``reference`` is a prepared ``IdentityReference`` for ``source``; when it is
    not given the source's faces are detected and embedded here.
    """
    threshold = float(settings.IDENTITY_GUARD_MIN_SIMILARITY)
    metadata = {
        "requested": True,
        "applied": False,
        "status": "skipped",
        "threshold": threshold,
        "faces_compared": 0,
        "similarity": None,
        "initial_similarity": None,
        "strength": 1.0,
        "attempts": [],
        "fallback_reason": "",
        "model": "sface_embedding_similarity",
    }
    if reference is None:
        reference, reason = build_identity_reference(source)
        if reference is None:
            metadata["reference_checked"] = True
            metadata["fallback_reason"] = reason
            return candidate, metadata
    metadata["reference_checked"] = True
    source_records = reference.records
    detector, provider = reference.detector, reference.provider
    metadata["faces_compared"] = len(source_records)

    height, width = reference.height, reference.width
    reference = source
    if reference.shape[:2] != candidate.shape[:2]:
        reference = cv2.resize(
            source, (candidate.shape[1], candidate.shape[0]), interpolation=cv2.INTER_CUBIC
        )

    trial = candidate
    for strength in settings.IDENTITY_GUARD_STRENGTHS or (1.0,):
        strength = min(max(float(strength), 0.0), 1.0)
        trial = (
            candidate
            if strength >= 1.0
            else cv2.addWeighted(candidate, strength, reference, 1.0 - strength, 0)
        )
        comparable = cv2.resize(trial, (width, height), interpolation=cv2.INTER_AREA)
        measured = _worst_face_similarity(source_records, comparable, detector, provider)
        if measured is None:
            first_attempt = not metadata["attempts"]
            metadata["attempts"].append({"strength": round(strength, 3), "similarity": None})
            if first_attempt:
                metadata["status"] = "inconclusive"
                metadata["fallback_reason"] = (
                    "The enhanced face could not be re-detected, so identity could not "
                    "be verified; the result was left unchanged."
                )
                return candidate, metadata
            continue
        similarity = round(float(measured), 4)
        metadata["attempts"].append({"strength": round(strength, 3), "similarity": similarity})
        if metadata["initial_similarity"] is None:
            metadata["initial_similarity"] = similarity
        metadata["strength"] = round(strength, 3)
        metadata["similarity"] = similarity
        if similarity >= threshold:
            metadata["status"] = "preserved" if strength >= 1.0 else "strength_reduced"
            metadata["applied"] = strength < 1.0
            return trial, metadata

    metadata["status"] = "unresolved"
    metadata["applied"] = True
    metadata["fallback_reason"] = (
        "Identity similarity stayed below the minimum at every strength; "
        "the most conservative blend was returned."
    )
    return trial, metadata

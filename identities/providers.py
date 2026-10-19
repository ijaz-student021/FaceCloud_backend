import hashlib
import time
from abc import ABC, abstractmethod
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from rest_framework.exceptions import ValidationError


class IdentityEmbeddingProvider(ABC):
    name = "abstract"
    model_checksum = ""
    dimension = 0
    # Similarity scales differ between models, so thresholds belong to the provider. ``None`` means "use the
    # SFace-calibrated settings" (IDENTITY_GUARD_MIN_SIMILARITY, IDENTITY_MATCH_THRESHOLD, ...).
    guard_min_similarity = None
    match_threshold = None
    new_group_threshold = None
    ambiguity_margin = None
    # The similarity at which two of one person's folders are suggested for merging; ``None`` = this provider's match threshold.
    merge_suggestion_similarity = None
    embedding_model_label = "sface_embedding_similarity"

    def thresholds(self):
        """``{guard, match, new_group, margin}`` for this provider, falling back to the settings."""
        pick = lambda own, fallback: float(fallback if own is None else own)  # noqa: E731
        return {
            "guard": pick(self.guard_min_similarity, settings.IDENTITY_GUARD_MIN_SIMILARITY),
            "match": pick(self.match_threshold, settings.IDENTITY_MATCH_THRESHOLD),
            "new_group": pick(self.new_group_threshold, settings.IDENTITY_NEW_GROUP_THRESHOLD),
            "margin": pick(self.ambiguity_margin, settings.IDENTITY_AMBIGUITY_MARGIN),
        }

    @abstractmethod
    def healthcheck(self): ...

    def prepare_face(self, image, box, landmarks=None):
        x, y, width, height = (int(box[key]) for key in ("x", "y", "width", "height"))
        x, y = max(0, x), max(0, y)
        crop = image[y:y + height, x:x + width]
        if crop.size == 0:
            raise ValidationError("A validated face crop is invalid.")
        return cv2.resize(crop, (160, 160), interpolation=cv2.INTER_AREA)

    @abstractmethod
    def embed(self, prepared_face): ...

    def validate_embedding(self, vector):
        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        if vector.size != self.dimension or not np.isfinite(vector).all():
            raise ValidationError("The identity provider returned an invalid embedding.")
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            raise ValidationError("The identity provider returned an invalid embedding.")
        return vector / norm

    def similarity(self, first, second):
        return float(np.dot(self.validate_embedding(first), self.validate_embedding(second)))


def thresholds_for(provider):
    """The provider's thresholds, or the settings' when it does not define them (a stand-in provider, for instance)."""
    keys = ("guard", "match", "new_group", "margin")
    try:
        values = provider.thresholds()
        if isinstance(values, dict) and all(isinstance(values.get(key), (int, float)) for key in keys):
            return values
    except (AttributeError, TypeError):
        pass
    return {
        "guard": float(settings.IDENTITY_GUARD_MIN_SIMILARITY), "match": float(settings.IDENTITY_MATCH_THRESHOLD),
        "new_group": float(settings.IDENTITY_NEW_GROUP_THRESHOLD), "margin": float(settings.IDENTITY_AMBIGUITY_MARGIN),
    }


def merge_suggestion_minimum(provider=None):
    """How alike two of one person's folders must be for the Identity Review screen to suggest merging them.

    ``MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY`` when it is set; otherwise the provider's own figure, so the suggestion is on the same scale as the
    grouping itself (a 0.75 chosen for one model means nothing for another).
    """
    explicit = settings.MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY
    if explicit is not None:
        return float(explicit)
    provider = provider or get_identity_provider()
    own = getattr(provider, "merge_suggestion_similarity", None)
    return float(own if own is not None else thresholds_for(provider)["match"])


class DisabledIdentityProvider(IdentityEmbeddingProvider):
    name = "disabled"
    dimension = settings.IDENTITY_EMBEDDING_DIMENSION

    def healthcheck(self):
        raise ValidationError("Identity inference is unavailable until an approved provider is configured.")

    def embed(self, prepared_face):
        self.healthcheck()


class SFaceOnnxProvider(IdentityEmbeddingProvider):
    """Local OpenCV SFace adapter; it never downloads model artifacts."""

    name = "sface_2021dec_v1"

    def __init__(self):
        self.dimension = settings.IDENTITY_EMBEDDING_DIMENSION
        self.model_checksum = settings.IDENTITY_MODEL_SHA256.lower()
        # SFace's own limits (measured, see settings.SFACE_*), not the generic ones; a subclass that sets its own keeps them.
        for attribute, name in (
            ("guard_min_similarity", "SFACE_GUARD_MIN_SIMILARITY"), ("match_threshold", "SFACE_MATCH_THRESHOLD"),
            ("new_group_threshold", "SFACE_NEW_GROUP_THRESHOLD"), ("ambiguity_margin", "SFACE_AMBIGUITY_MARGIN"),
            ("merge_suggestion_similarity", "SFACE_MERGE_SUGGESTION_MIN_SIMILARITY"),
        ):
            if getattr(self, attribute) is None:
                setattr(self, attribute, getattr(settings, name))
        self._recognizer = None

    def healthcheck(self):
        verify_model_checksum(settings.IDENTITY_MODEL_PATH, self.model_checksum)
        if not Path(settings.IDENTITY_LICENSE_RECORD_PATH).is_file():
            raise ValidationError("The identity model license record is unavailable.")
        if self._recognizer is None:
            try:
                self._recognizer = cv2.FaceRecognizerSF_create(
                    settings.IDENTITY_MODEL_PATH, ""
                )
            except cv2.error as error:
                raise ValidationError("The configured identity model cannot be loaded safely.") from error
        return True

    def prepare_face(self, image, box, landmarks=None):
        self.healthcheck()
        if not landmarks or len(landmarks) != 5:
            raise ValidationError("SFace requires five YuNet alignment landmarks.")
        points = []
        for point in landmarks:
            if isinstance(point, dict):
                points.extend([float(point["x"]), float(point["y"])])
            else:
                points.extend([float(point[0]), float(point[1])])
        face_row = np.asarray([
            float(box["x"]), float(box["y"]), float(box["width"]),
            float(box["height"]), *points, 1.0,
        ], dtype=np.float32)
        try:
            return self._recognizer.alignCrop(image, face_row)
        except (cv2.error, KeyError, TypeError, ValueError) as error:
            raise ValidationError("The validated face could not be aligned safely.") from error

    def embed(self, prepared_face):
        self.healthcheck()
        try:
            return self.validate_embedding(
                self._recognizer.feature(prepared_face).reshape(-1)
            )
        except cv2.error as error:
            raise ValidationError("SFace feature extraction failed safely.") from error


# The five-point template ArcFace was trained on, for a 112 px aligned face.
_ARCFACE_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32,
)


class ArcFaceOnnxProvider(IdentityEmbeddingProvider):
    """ArcFace (InsightFace ``w600k_r50``, 512-d) through OpenCV's DNN module; it never downloads model artifacts.

    Landmarks come from the same YuNet detector SFace uses. The pretrained weights are licensed for
    non-commercial research use only (docs/model-licenses/model-set-2026-10.md), which a license record must confirm.
    """

    name = "arcface_w600k_r50_v1"
    dimension = 512
    embedding_model_label = "arcface_embedding_similarity"

    def __init__(self):
        self.model_checksum = settings.ARCFACE_MODEL_SHA256.lower()
        self.guard_min_similarity = settings.ARCFACE_GUARD_MIN_SIMILARITY
        self.match_threshold = settings.ARCFACE_MATCH_THRESHOLD
        self.new_group_threshold = settings.ARCFACE_NEW_GROUP_THRESHOLD
        self.ambiguity_margin = settings.ARCFACE_AMBIGUITY_MARGIN
        self._net = None

    def healthcheck(self):
        try:
            verify_model_checksum(settings.ARCFACE_MODEL_PATH, self.model_checksum)
        except OSError as error:
            raise ValidationError("The ArcFace model is unavailable.") from error
        if not Path(settings.ARCFACE_LICENSE_RECORD_PATH).is_file():
            raise ValidationError("The ArcFace model license record is unavailable.")
        if self._net is None:
            try:
                self._net = cv2.dnn.readNetFromONNX(settings.ARCFACE_MODEL_PATH)
            except cv2.error as error:
                raise ValidationError("The configured ArcFace model cannot be loaded safely.") from error
        return True

    def prepare_face(self, image, box, landmarks=None):
        if not landmarks or len(landmarks) != 5:
            raise ValidationError("ArcFace requires five YuNet alignment landmarks.")
        points = np.array(
            [[float(p["x"]), float(p["y"])] if isinstance(p, dict) else [float(p[0]), float(p[1])] for p in landmarks],
            dtype=np.float32,
        )
        matrix, _inliers = cv2.estimateAffinePartial2D(points, _ARCFACE_TEMPLATE, method=cv2.LMEDS)
        if matrix is None:
            raise ValidationError("The validated face could not be aligned safely.")
        return cv2.warpAffine(image, matrix, (112, 112), flags=cv2.INTER_LINEAR, borderValue=0)

    def embed(self, prepared_face):
        self.healthcheck()
        try:
            blob = cv2.dnn.blobFromImage(prepared_face, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
            self._net.setInput(blob)
            return self.validate_embedding(self._net.forward().reshape(-1))
        except cv2.error as error:
            raise ValidationError("ArcFace feature extraction failed safely.") from error


def get_identity_provider():
    if settings.IDENTITY_PROVIDER == SFaceOnnxProvider.name:
        return SFaceOnnxProvider()
    if settings.IDENTITY_PROVIDER == ArcFaceOnnxProvider.name:
        return ArcFaceOnnxProvider()
    return DisabledIdentityProvider()


# Successful verifications, keyed by the file's identity on disk. Hashing a 38 MB
# model on every call cost a full read per face, so a result is reused, with two
# limits that keep the check honest:
#
# * A file modified less than RACY_SECONDS ago is never cached. File timestamps
#   are coarse (a same-size rewrite in the same tick keeps the same mtime), so only
#   a file old enough that any later change must show a different mtime is trusted.
# * A cached result expires after RECHECK_SECONDS, so even a forged size and mtime
#   is caught within that time.
#
# Failures are never cached, and a change to the path, size or mtime is a miss.
_VERIFIED_MODELS = {}
RACY_SECONDS = 2
RECHECK_SECONDS = 600


def verify_model_checksum(path, expected):
    model_path = Path(path)
    stat = model_path.stat()
    key = (str(model_path.resolve()), stat.st_size, stat.st_mtime_ns, (expected or "").lower())
    cached = _VERIFIED_MODELS.get(key)
    if cached is not None and time.monotonic() - cached[1] < RECHECK_SECONDS:
        return cached[0]
    hasher = hashlib.sha256()
    with model_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if not expected or digest.lower() != expected.lower():
        raise ValidationError("The configured identity model failed integrity verification.")
    if time.time_ns() - stat.st_mtime_ns > RACY_SECONDS * 1_000_000_000:
        _VERIFIED_MODELS[key] = (digest, time.monotonic())
    return digest

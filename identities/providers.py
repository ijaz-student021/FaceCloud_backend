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


_provider_override = None


def set_provider_for_tests(provider):
    global _provider_override
    _provider_override = provider


def get_identity_provider():
    if _provider_override is not None:
        return _provider_override
    if settings.IDENTITY_PROVIDER == SFaceOnnxProvider.name:
        return SFaceOnnxProvider()
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

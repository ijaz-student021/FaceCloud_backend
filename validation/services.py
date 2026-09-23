import time
from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult, ImageUpload

from .models import FaceValidationResult


MIN_FACE_AREA_RATIO = 0.06
MIN_GROUP_FACE_AREA_RATIO = 0.012
MAX_SUPPORTED_FACES = 5
CENTER_TOLERANCE = 0.28
HAAR_DETECTOR_NAME = "opencv_haar_frontalface"
HAAR_DETECTOR_VERSION = "1"
HEURISTIC_CONFIDENCE_TYPE = "internal_heuristic_not_probability"
FACE_METRICS_VERSION = "full_resolution_face_metrics_v1"


@dataclass(frozen=True)
class FaceDetection:
    """Detector-neutral output with explicit score and landmark provenance."""

    boxes: list
    scores: list
    landmarks: list
    landmark_scores: list
    detector: str
    detector_version: str
    confidence_type: str


class OpenCVHaarFaceDetector:
    """Compatibility adapter for the bundled, commercially simple baseline."""

    name = HAAR_DETECTOR_NAME
    version = HAAR_DETECTOR_VERSION
    confidence_type = HEURISTIC_CONFIDENCE_TYPE

    def detect(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        boxes, scores = _detect(gray)
        count = len(boxes)
        return FaceDetection(
            boxes=[tuple(int(value) for value in box) for box in boxes],
            scores=list(scores),
            landmarks=[None] * count,
            landmark_scores=[None] * count,
            detector=self.name,
            detector_version=self.version,
            confidence_type=self.confidence_type,
        )


class OpenCVYuNetFaceDetector:
    name = "opencv_yunet"
    version = "s_dynamic_v1"
    confidence_type = "yunet_detection_score"

    def __init__(self):
        from identities.providers import verify_model_checksum

        verify_model_checksum(settings.YUNET_MODEL_PATH, settings.YUNET_MODEL_SHA256)
        try:
            self.detector = cv2.FaceDetectorYN_create(
                settings.YUNET_MODEL_PATH,
                "",
                (320, 320),
                settings.YUNET_SCORE_THRESHOLD,
                settings.YUNET_NMS_THRESHOLD,
                settings.YUNET_TOP_K,
            )
        except cv2.error as error:
            raise ValidationError("The configured YuNet detector cannot be loaded safely.") from error

    def detect(self, image):
        if image is None or image.size == 0:
            raise ValidationError("The image cannot be inspected for faces safely.")
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        try:
            _result, rows = self.detector.detect(image)
        except cv2.error as error:
            raise ValidationError("YuNet face detection failed safely.") from error
        rows = [] if rows is None else rows
        boxes, scores, landmarks = [], [], []
        for row in rows:
            boxes.append(tuple(int(round(value)) for value in row[:4]))
            landmarks.append([
                (float(row[offset]), float(row[offset + 1]))
                for offset in range(4, 14, 2)
            ])
            scores.append(round(float(row[14]), 6))
        return FaceDetection(
            boxes=boxes,
            scores=scores,
            landmarks=landmarks,
            landmark_scores=list(scores),
            detector=self.name,
            detector_version=self.version,
            confidence_type=self.confidence_type,
        )


def get_face_detector():
    """Return the configured detector through a replaceable adapter contract."""
    if settings.FACE_DETECTOR_PROVIDER == "yunet_v1":
        return OpenCVYuNetFaceDetector()
    return OpenCVHaarFaceDetector()


def _validate_detection_contract(detection):
    if not isinstance(detection, FaceDetection):
        raise ValidationError(
            "The configured face detector returned an unsupported result."
        )
    expected = len(detection.boxes)
    lengths = {
        len(detection.scores),
        len(detection.landmarks),
        len(detection.landmark_scores),
    }
    if lengths != {expected}:
        raise ValidationError(
            "The configured face detector returned inconsistent face metadata."
        )
    if any(len(box) != 4 for box in detection.boxes):
        raise ValidationError(
            "The configured face detector returned an invalid bounding box."
        )


def _read_image(preprocessing):
    if preprocessing.status != "completed" or not preprocessing.processed_file:
        raise ValidationError("The image must finish preprocessing before face validation.")
    preprocessing.processed_file.open("rb")
    try:
        payload = preprocessing.processed_file.read()
    finally:
        preprocessing.processed_file.close()
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValidationError("The standardized image could not be decoded for face validation.")
    return image


def _read_full_resolution_original(image_upload):
    """Decode the EXIF-oriented original without resizing the user's pixels."""
    if not image_upload.original_file:
        raise ValidationError("The original image is unavailable for face metrics.")
    image_upload.original_file.open("rb")
    try:
        with Image.open(image_upload.original_file) as source:
            source.load()
            working = ImageOps.exif_transpose(source)
            if working.mode in {"RGBA", "LA"} or (
                working.mode == "P" and "transparency" in working.info
            ):
                rgba = working.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                working = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                working = working.convert("RGB")
            rgb = np.asarray(working)
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as error:
        raise ValidationError(
            "The original image cannot be decoded for face metrics."
        ) from error
    finally:
        image_upload.original_file.close()
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _detect(gray):
    cascade_path = Path(__file__).resolve().parent / "assets" / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():
        raise ValidationError("The configured face detector is unavailable.")
    if hasattr(detector, "detectMultiScale3"):
        boxes, _reject_levels, weights = detector.detectMultiScale3(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40), outputRejectLevels=True,
        )
        return boxes, [round(max(0.0, min(1.0, float(weight) / 10.0)), 4) for weight in weights]
    boxes = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
    return boxes, [None] * len(boxes)


def _map_point(point, scale_x, scale_y, target_width, target_height):
    x = max(0, min(target_width - 1, round(float(point[0]) * scale_x)))
    y = max(0, min(target_height - 1, round(float(point[1]) * scale_y)))
    return {"x": int(x), "y": int(y)}


def _map_box_to_full_resolution(
    box,
    source_width,
    source_height,
    target_width,
    target_height,
):
    """Map a standardized detector box onto the oriented original grid."""
    x, y, width, height = [int(value) for value in box]
    scale_x = target_width / float(source_width)
    scale_y = target_height / float(source_height)
    left = max(0, min(target_width - 1, floor(x * scale_x)))
    top = max(0, min(target_height - 1, floor(y * scale_y)))
    right = max(left + 1, min(target_width, ceil((x + width) * scale_x)))
    bottom = max(top + 1, min(target_height, ceil((y + height) * scale_y)))
    boundary_clipped = (
        x <= 0
        or y <= 0
        or x + width >= source_width
        or y + height >= source_height
        or left <= 0
        or top <= 0
        or right >= target_width
        or bottom >= target_height
    )
    return {
        "x": int(left),
        "y": int(top),
        "width": int(right - left),
        "height": int(bottom - top),
        "boundary_clipped": boundary_clipped,
    }


def _measure_full_resolution_face(image, mapped_box):
    x = mapped_box["x"]
    y = mapped_box["y"]
    width = mapped_box["width"]
    height = mapped_box["height"]
    crop = image[y:y + height, x:x + width]
    if crop.size == 0:
        return {
            "status": "unavailable",
            "reason": "mapped_face_crop_is_empty",
        }
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return {
        "status": "measured",
        "blur_score_laplacian_variance": round(
            float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            3,
        ),
        "mean_luminance": round(float(np.mean(gray)), 3),
        "highlight_clipping_percent": round(
            float(np.mean(gray >= 250) * 100),
            4,
        ),
        "crop_width": int(width),
        "crop_height": int(height),
    }


def _enrich_face_with_full_resolution_metrics(
    face,
    box,
    landmarks,
    landmark_score,
    full_resolution_image,
    source_width,
    source_height,
):
    target_height, target_width = full_resolution_image.shape[:2]
    mapped_box = _map_box_to_full_resolution(
        box,
        source_width,
        source_height,
        target_width,
        target_height,
    )
    face["standardized_box"] = {
        "x": face["x"],
        "y": face["y"],
        "width": face["width"],
        "height": face["height"],
    }
    face["full_resolution_box"] = mapped_box
    face["boundary_clipped"] = mapped_box["boundary_clipped"]
    face["quality_metrics"] = _measure_full_resolution_face(
        full_resolution_image,
        mapped_box,
    )
    scale_x = target_width / float(source_width)
    scale_y = target_height / float(source_height)
    face["landmarks"] = (
        [
            _map_point(
                point,
                scale_x,
                scale_y,
                target_width,
                target_height,
            )
            for point in landmarks
        ]
        if landmarks
        else []
    )
    face["landmark_confidence"] = landmark_score
    if landmarks:
        face["pose_occlusion"] = {
            "status": "not_evaluated",
            "reason": "pose_model_not_configured",
        }
    else:
        face["pose_occlusion"] = {
            "status": "unavailable",
            "reason": "detector_does_not_provide_landmarks",
        }
    return face


@transaction.atomic
def validate_image_faces(image_upload):
    image_upload = ImageUpload.objects.select_for_update().select_related(
        "owner"
    ).get(pk=image_upload.pk)
    try:
        preprocessing = image_upload.preprocessing_result
    except ImagePreprocessingResult.DoesNotExist as error:
        raise ValidationError("The image must finish preprocessing before face validation.") from error

    started = time.perf_counter()
    image = _read_image(preprocessing)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    detection = get_face_detector().detect(image)
    _validate_detection_contract(detection)
    height, width = gray.shape
    image_area = float(width * height)
    faces = []
    for box, confidence, landmarks, landmark_score in zip(
        detection.boxes,
        detection.scores,
        detection.landmarks,
        detection.landmark_scores,
    ):
        x, y, face_width, face_height = [int(value) for value in box]
        face = {
            "x": x, "y": y, "width": face_width, "height": face_height,
            "area_ratio": round((face_width * face_height) / image_area, 4),
            "confidence": confidence,
            "standardized_box": {
                "x": x, "y": y, "width": face_width, "height": face_height,
            },
            "standardized_landmarks": [
                {"x": round(float(point[0]), 3), "y": round(float(point[1]), 3)}
                for point in (landmarks or [])
            ],
        }
        if settings.PER_FACE_METRICS_ENABLED:
            face["confidence_type"] = detection.confidence_type
        faces.append((face, box, landmarks, landmark_score))

    full_resolution_image = None
    mapping = {}
    metrics_enabled = bool(settings.PER_FACE_METRICS_ENABLED)
    metrics_available = False
    if metrics_enabled:
        try:
            full_resolution_image = _read_full_resolution_original(image_upload)
            full_height, full_width = full_resolution_image.shape[:2]
            mapping = {
                "status": "mapped",
                "method": "axis_scale_after_exif_orientation",
                "source": "standardized_image",
                "target": "oriented_full_resolution_original",
                "source_dimensions": [int(width), int(height)],
                "target_dimensions": [int(full_width), int(full_height)],
                "scale_x": round(full_width / float(width), 8),
                "scale_y": round(full_height / float(height), 8),
            }
            metrics_available = True
        except ValidationError as error:
            mapping = {
                "status": "unavailable",
                "method": "axis_scale_after_exif_orientation",
                "reason": str(error.detail if hasattr(error, "detail") else error),
            }

    enriched_faces = []
    for face, box, landmarks, landmark_score in faces:
        if metrics_available:
            face = _enrich_face_with_full_resolution_metrics(
                face,
                box,
                landmarks,
                landmark_score,
                full_resolution_image,
                width,
                height,
            )
        elif metrics_enabled:
            face.update({
                "confidence_type": detection.confidence_type,
                "standardized_box": {
                    "x": face["x"],
                    "y": face["y"],
                    "width": face["width"],
                    "height": face["height"],
                },
                "full_resolution_box": None,
                "boundary_clipped": None,
                "quality_metrics": {
                    "status": "unavailable",
                    "reason": "full_resolution_original_unavailable",
                },
                "landmarks": [],
                "landmark_confidence": None,
                "pose_occlusion": {
                    "status": "unavailable",
                    "reason": "detector_does_not_provide_landmarks",
                },
            })
        else:
            face["landmarks"] = face["standardized_landmarks"]
            face["landmark_confidence"] = landmark_score
        enriched_faces.append(face)
    faces = enriched_faces
    faces.sort(key=lambda face: face["area_ratio"], reverse=True)

    reason = ""
    if not faces:
        decision = FaceValidationResult.Decision.NEEDS_REVIEW
        reason = "no_face_detected"
        message = "No confident frontal face was detected. Manual review is required."
    elif len(faces) > MAX_SUPPORTED_FACES:
        decision = FaceValidationResult.Decision.NEEDS_REVIEW
        reason = "large_group_detected"
        message = "More than five faces were detected. Manual review is required."
    elif len(faces) >= 2:
        if any(face["area_ratio"] < MIN_GROUP_FACE_AREA_RATIO for face in faces):
            decision = FaceValidationResult.Decision.NEEDS_REVIEW
            reason = "group_face_too_small"
            message = "A group image was detected, but one or more faces are too small for reliable processing."
        else:
            decision = FaceValidationResult.Decision.ACCEPTED
            message = f"A valid group image containing {len(faces)} people was detected."
    elif faces[0]["area_ratio"] < MIN_FACE_AREA_RATIO:
        decision = FaceValidationResult.Decision.REJECTED
        reason = "face_too_small"
        message = "The detected face is too small for reliable enhancement."
    else:
        face = faces[0]
        face_center_x = face["x"] + face["width"] / 2
        face_center_y = face["y"] + face["height"] / 2
        off_center = (
            abs(face_center_x / width - 0.5) > CENTER_TOLERANCE
            or abs(face_center_y / height - 0.5) > CENTER_TOLERANCE
        )
        if off_center:
            decision = FaceValidationResult.Decision.NEEDS_REVIEW
            reason = "face_off_center"
            message = "A face was detected near the image boundary. Manual review is required."
        else:
            decision = FaceValidationResult.Decision.ACCEPTED
            message = "Exactly one suitable frontal face was detected."

    result, _created = FaceValidationResult.objects.update_or_create(
        image=image_upload,
        defaults={
            "owner": image_upload.owner,
            "preprocessing_result": preprocessing,
            "decision": decision,
            "face_count": len(faces),
            "primary_confidence": faces[0]["confidence"] if faces else None,
            "primary_confidence_type": detection.confidence_type,
            "faces": faces,
            "rejection_reason": reason,
            "message": message,
            "detector": detection.detector,
            "detector_version": detection.detector_version,
            "analysis_source": (
                "standardized_detection_full_resolution_metrics"
                if metrics_available
                else "standardized_image"
            ),
            "coordinate_mapping": mapping,
            "per_face_metrics_enabled": metrics_enabled,
            "full_resolution_width": (
                int(full_resolution_image.shape[1])
                if metrics_available
                else None
            ),
            "full_resolution_height": (
                int(full_resolution_image.shape[0])
                if metrics_available
                else None
            ),
            "metrics_version": FACE_METRICS_VERSION if metrics_enabled else "",
            "processing_duration_ms": max(1, round((time.perf_counter() - started) * 1000)),
        },
    )
    return result


def validate_image_faces_safely(image_upload):
    try:
        return validate_image_faces(image_upload)
    except Exception:
        try:
            preprocessing = image_upload.preprocessing_result
        except ImagePreprocessingResult.DoesNotExist:
            return None
        result, _created = FaceValidationResult.objects.update_or_create(
            image=image_upload,
            defaults={
                "owner": image_upload.owner,
                "preprocessing_result": preprocessing,
                "decision": FaceValidationResult.Decision.FAILED,
                "face_count": 0,
                "primary_confidence": None,
                "primary_confidence_type": HEURISTIC_CONFIDENCE_TYPE,
                "faces": [],
                "rejection_reason": "detector_failed",
                "message": "Face validation could not be completed safely.",
                "detector": HAAR_DETECTOR_NAME,
                "detector_version": HAAR_DETECTOR_VERSION,
                "analysis_source": "standardized_image",
                "coordinate_mapping": {},
                "per_face_metrics_enabled": bool(
                    settings.PER_FACE_METRICS_ENABLED
                ),
                "full_resolution_width": None,
                "full_resolution_height": None,
                "metrics_version": (
                    FACE_METRICS_VERSION
                    if settings.PER_FACE_METRICS_ENABLED
                    else ""
                ),
                "processing_duration_ms": 0,
            },
        )
        return result

import time
from pathlib import Path

import cv2
import numpy as np
from django.db import transaction
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult, ImageUpload

from .models import FaceValidationResult


MIN_FACE_AREA_RATIO = 0.06
MIN_GROUP_FACE_AREA_RATIO = 0.012
MAX_SUPPORTED_FACES = 5
CENTER_TOLERANCE = 0.28


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
    boxes, confidences = _detect(gray)
    height, width = gray.shape
    image_area = float(width * height)
    faces = []
    for box, confidence in zip(boxes, confidences):
        x, y, face_width, face_height = [int(value) for value in box]
        faces.append({
            "x": x, "y": y, "width": face_width, "height": face_height,
            "area_ratio": round((face_width * face_height) / image_area, 4),
            "confidence": confidence,
        })
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
            "faces": faces,
            "rejection_reason": reason,
            "message": message,
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
                "faces": [],
                "rejection_reason": "detector_failed",
                "message": "Face validation could not be completed safely.",
            },
        )
        return result

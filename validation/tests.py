import tempfile
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .services import (
    HEURISTIC_CONFIDENCE_TYPE,
    FaceDetection,
    OpenCVYuNetFaceDetector,
    _validate_detection_contract,
    _detect,
    validate_image_faces,
)

User = get_user_model()


class FaceValidationTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.override = override_settings(
            MEDIA_ROOT=self.media.name,
            FACE_DETECTOR_PROVIDER="haar_v1",
        )
        self.override.enable()
        self.user = User.objects.create_user(
            username="face-owner", email="face-owner@example.com",
            password="StrongPass!4729", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="face.jpg", status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=self.image, status="completed",
            output_name="face.jpg", output_format="jpeg",
        )
        import cv2
        ok, encoded = cv2.imencode(".jpg", np.full((400, 400, 3), 128, dtype=np.uint8))
        self.assertTrue(ok)
        self.preprocessing.processed_file.save("face.jpg", ContentFile(encoded.tobytes()))
        token = RefreshToken.for_user(self.user)
        token["role"] = self.user.role
        token["token_version"] = self.user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def tearDown(self):
        self.override.disable()
        self.media.cleanup()

    def _save_original(self, image):
        import cv2

        ok, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        self.image.original_file.save(
            "original.jpg",
            ContentFile(encoded.tobytes()),
        )

    def test_bundled_detector_asset_loads(self):
        boxes, confidences = _detect(np.full((120, 120), 128, dtype=np.uint8))
        self.assertEqual(len(boxes), len(confidences))

    @override_settings(FACE_DETECTOR_PROVIDER="yunet_v1")
    def test_installed_yunet_model_supports_dynamic_input_and_contract(self):
        from pathlib import Path

        from django.conf import settings

        if not Path(settings.YUNET_MODEL_PATH).is_file():
            self.skipTest("The YuNet model is not installed here.")
        detector = OpenCVYuNetFaceDetector()
        for width, height in ((320, 320), (640, 480)):
            result = detector.detect(
                np.full((height, width, 3), 127, dtype=np.uint8)
            )
            _validate_detection_contract(result)
            self.assertEqual(result.detector, "opencv_yunet")
            self.assertEqual(len(result.boxes), len(result.landmarks))

    def test_detector_contract_rejects_mismatched_metadata(self):
        with self.assertRaisesMessage(
            ValidationError,
            "inconsistent face metadata",
        ):
            _validate_detection_contract(
                FaceDetection(
                    boxes=[(10, 10, 80, 80)],
                    scores=[],
                    landmarks=[None],
                    landmark_scores=[None],
                    detector="invalid-test-detector",
                    detector_version="test",
                    confidence_type="test",
                )
            )

    @patch("validation.services._detect")
    def test_one_suitable_face_is_accepted(self, detector):
        detector.return_value = (np.array([[100, 80, 180, 180]]), [0.92])
        result = validate_image_faces(self.image)
        self.assertEqual(result.decision, "accepted")
        self.assertEqual(result.face_count, 1)
        self.assertEqual(result.rejection_reason, "")
        self.assertEqual(
            result.primary_confidence_type,
            HEURISTIC_CONFIDENCE_TYPE,
        )
        self.assertFalse(result.per_face_metrics_enabled)
        self.assertEqual(
            set(result.faces[0]),
            {
                "x", "y", "width", "height", "area_ratio", "confidence",
                "standardized_box", "standardized_landmarks", "landmarks",
                "landmark_confidence",
            },
        )

    @override_settings(PER_FACE_METRICS_ENABLED=True)
    @patch("validation.services._detect")
    def test_full_resolution_face_metrics_use_mapped_original_crop(
        self,
        detector,
    ):
        detector.return_value = (np.array([[100, 80, 180, 180]]), [0.92])
        full_resolution = np.full((800, 800, 3), 45, dtype=np.uint8)
        full_resolution[160:520, 200:560] = 215
        self._save_original(full_resolution)

        result = validate_image_faces(self.image)

        face = result.faces[0]
        self.assertTrue(result.per_face_metrics_enabled)
        self.assertEqual(
            result.analysis_source,
            "standardized_detection_full_resolution_metrics",
        )
        self.assertEqual(result.full_resolution_width, 800)
        self.assertEqual(result.full_resolution_height, 800)
        self.assertEqual(
            result.coordinate_mapping["source_dimensions"],
            [400, 400],
        )
        self.assertEqual(
            result.coordinate_mapping["target_dimensions"],
            [800, 800],
        )
        self.assertEqual(
            face["standardized_box"],
            {"x": 100, "y": 80, "width": 180, "height": 180},
        )
        self.assertEqual(
            face["full_resolution_box"],
            {
                "x": 200,
                "y": 160,
                "width": 360,
                "height": 360,
                "boundary_clipped": False,
            },
        )
        self.assertEqual(face["quality_metrics"]["status"], "measured")
        self.assertGreater(face["quality_metrics"]["mean_luminance"], 200)
        self.assertEqual(face["confidence_type"], HEURISTIC_CONFIDENCE_TYPE)
        self.assertEqual(face["landmarks"], [])
        self.assertEqual(
            face["pose_occlusion"]["status"],
            "unavailable",
        )

    @override_settings(PER_FACE_METRICS_ENABLED=True)
    @patch("validation.services.get_face_detector")
    def test_detector_landmarks_are_mapped_without_claiming_pose(
        self,
        get_detector,
    ):
        detector = get_detector.return_value
        detector.detect.return_value = FaceDetection(
            boxes=[(100, 80, 180, 180)],
            scores=[0.88],
            landmarks=[[
                (120, 120),
                (220, 120),
                (170, 165),
                (135, 220),
                (205, 220),
            ]],
            landmark_scores=[0.81],
            detector="licensed_landmark_adapter",
            detector_version="test",
            confidence_type="native_detector_score_not_calibrated_probability",
        )
        self._save_original(np.full((800, 800, 3), 128, dtype=np.uint8))

        result = validate_image_faces(self.image)

        face = result.faces[0]
        self.assertEqual(
            face["landmarks"][0],
            {"x": 240, "y": 240},
        )
        self.assertEqual(face["landmark_confidence"], 0.81)
        self.assertEqual(face["pose_occlusion"]["status"], "not_evaluated")
        self.assertEqual(
            face["pose_occlusion"]["reason"],
            "pose_model_not_configured",
        )
        self.assertEqual(
            result.primary_confidence_type,
            "native_detector_score_not_calibrated_probability",
        )

    @override_settings(PER_FACE_METRICS_ENABLED=True)
    @patch("validation.services._detect")
    def test_missing_original_records_unavailable_metrics_without_failure(
        self,
        detector,
    ):
        detector.return_value = (np.array([[100, 80, 180, 180]]), [0.92])

        result = validate_image_faces(self.image)

        self.assertEqual(result.decision, "accepted")
        self.assertTrue(result.per_face_metrics_enabled)
        self.assertEqual(result.analysis_source, "standardized_image")
        self.assertEqual(result.coordinate_mapping["status"], "unavailable")
        self.assertIsNone(result.faces[0]["full_resolution_box"])
        self.assertEqual(
            result.faces[0]["quality_metrics"]["status"],
            "unavailable",
        )

    @override_settings(PER_FACE_METRICS_ENABLED=True)
    @patch("validation.services._detect")
    def test_face_touching_image_edge_is_marked_boundary_clipped(
        self,
        detector,
    ):
        detector.return_value = (np.array([[0, 50, 100, 100]]), [0.75])
        self._save_original(np.full((800, 800, 3), 128, dtype=np.uint8))

        result = validate_image_faces(self.image)

        self.assertTrue(result.faces[0]["boundary_clipped"])
        self.assertTrue(
            result.faces[0]["full_resolution_box"]["boundary_clipped"]
        )

    @patch("validation.services._detect")
    def test_no_face_requires_review_and_three_to_five_faces_are_allowed(self, detector):
        detector.return_value = (np.empty((0, 4), dtype=int), [])
        review = validate_image_faces(self.image)
        self.assertEqual(review.decision, "needs_review")
        self.assertEqual(review.rejection_reason, "no_face_detected")

        detector.return_value = (
            np.array([
                [20, 40, 80, 80], [115, 40, 80, 80], [210, 40, 80, 80],
                [70, 180, 80, 80], [190, 180, 80, 80],
            ]),
            [0.9, 0.88, 0.86, 0.84, 0.82],
        )
        accepted = validate_image_faces(self.image)
        self.assertEqual(accepted.decision, "accepted")
        self.assertEqual(accepted.face_count, 5)
        self.assertEqual(accepted.rejection_reason, "")

    @patch("validation.services._detect")
    def test_api_is_owner_scoped_and_returns_persisted_result(self, detector):
        detector.return_value = (np.array([[100, 80, 180, 180]]), [0.92])
        response = self.client.post(
            reverse("validation:image-validation", kwargs={"image_id": self.image.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["decision"], "accepted")
        self.assertEqual(
            response.data["primary_confidence_type"],
            HEURISTIC_CONFIDENCE_TYPE,
        )
        self.assertEqual(response.data["detector_version"], "1")
        self.assertFalse(response.data["per_face_metrics_enabled"])

        other = User.objects.create_user(
            username="face-other", email="face-other@example.com",
            password="StrongPass!4729", is_email_verified=True,
        )
        token = RefreshToken.for_user(other)
        token["role"] = other.role
        token["token_version"] = other.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        hidden = self.client.get(
            reverse("validation:image-validation", kwargs={"image_id": self.image.pk})
        )
        self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)

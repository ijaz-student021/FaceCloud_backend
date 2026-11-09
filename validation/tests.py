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
        if not settings.YUNET_MODEL_SHA256:
            self.skipTest("The YuNet model is installed but no checksum is configured, so it cannot be verified.")
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

    @staticmethod
    def faces(count, size, per_row=6):
        """``count`` boxes of ``size`` x ``size`` in a 400 x 400 frame, laid out in rows and clear of the edges."""
        return np.array([[20 + (i % per_row) * 62, 20 + (i // per_row) * 62, size, size] for i in range(count)])

    @patch("validation.services._detect")
    def test_a_group_photograph_is_accepted_whatever_the_number_of_faces(self, detector):
        for count in (2, 3, 5, 6, 8, 12, 20):
            with self.subTest(faces=count):
                detector.return_value = (self.faces(count, 50, per_row=6), [0.9] * count)
                result = validate_image_faces(self.image)
                self.assertEqual((result.decision, result.rejection_reason, result.face_count), ("accepted", "", count), result.message)

    @patch("validation.services._detect")
    def test_a_group_photograph_with_some_tiny_faces_is_accepted_and_says_how_many_are_skipped(self, detector):
        boxes = np.concatenate([self.faces(4, 50), np.array([[300, 300, 20, 20], [340, 300, 20, 20]])])
        detector.return_value = (boxes, [0.9] * 6)
        result = validate_image_faces(self.image)
        self.assertEqual((result.decision, result.rejection_reason), ("accepted", ""))
        self.assertIn("2 too small to group are skipped", result.message)

    @patch("validation.services._detect")
    def test_a_group_photograph_where_no_face_is_big_enough_still_goes_to_review(self, detector):
        detector.return_value = (self.faces(3, 20), [0.9] * 3)
        result = validate_image_faces(self.image)
        self.assertEqual((result.decision, result.rejection_reason), ("needs_review", "group_face_too_small"))

    @patch("validation.services._detect")
    def test_a_single_small_face_is_reviewed_not_rejected(self, detector):
        detector.return_value = (np.array([[180, 180, 40, 40]]), [0.9])
        result = validate_image_faces(self.image)
        self.assertEqual((result.decision, result.rejection_reason), ("needs_review", "face_too_small"))

    @patch("validation.services._detect")
    def test_a_portrait_with_the_face_in_the_upper_part_of_the_picture_is_accepted(self, detector):
        """A head-and-shoulders portrait: the face's centre is about 21% from the top. The earlier rule (centre within 0.28 of the middle) sent every such photograph to review."""
        detector.return_value = (np.array([[130, 8, 120, 150]]), [0.9])
        result = validate_image_faces(self.image)
        self.assertEqual((result.decision, result.rejection_reason), ("accepted", ""))
        self.assertLess((8 + 75) / 400, 0.22)                         # the face centre really is high in the frame: the old rule refused this

    @patch("validation.services._detect")
    def test_where_the_face_sits_does_not_matter_while_it_is_inside_the_frame(self, detector):
        for box in ((24, 24, 120, 150), (256, 24, 120, 150), (24, 226, 120, 150), (256, 226, 120, 150), (4, 100, 120, 150), (276, 100, 120, 150)):
            with self.subTest(box=box):
                detector.return_value = (np.array([box]), [0.9])
                result = validate_image_faces(self.image)
                self.assertEqual((result.decision, result.rejection_reason), ("accepted", ""), result.message)

    @patch("validation.services._detect")
    def test_a_face_touching_or_cut_by_an_edge_still_goes_to_review(self, detector):
        for name, box in (("top", (130, 0, 120, 150)), ("just inside the margin, top", (130, 3, 120, 150)), ("left", (0, 100, 120, 150)),
                          ("right", (280, 100, 120, 150)), ("bottom", (130, 260, 120, 150)), ("cut off at the right", (330, 100, 120, 150))):
            with self.subTest(case=name):
                detector.return_value = (np.array([box]), [0.9])
                result = validate_image_faces(self.image)
                self.assertEqual((result.decision, result.rejection_reason), ("needs_review", "face_off_center"), result.message)
                self.assertEqual(result.message, "A face was detected near the image boundary. Manual review is required.")

    def test_the_edge_rule_in_isolation_on_a_frame_that_is_not_square(self):
        from .services import face_touches_edge

        photo = {"x": 186, "y": 66, "width": 164, "height": 218}      # the 670 x 890 photograph this was found with: face centre 20% from the top
        self.assertFalse(face_touches_edge(photo, 670, 890))
        self.assertTrue(face_touches_edge({**photo, "y": 5}, 670, 890))                # 5 px is under 1% of 890
        self.assertFalse(face_touches_edge({**photo, "y": 9}, 670, 890))
        self.assertTrue(face_touches_edge({**photo, "x": 500}, 670, 890))              # runs past the right edge
        self.assertFalse(face_touches_edge({"x": 4, "y": 100, "width": 100, "height": 100}, 400, 400))     # exactly on the margin is not inside it
        self.assertTrue(face_touches_edge({"x": 3, "y": 100, "width": 100, "height": 100}, 400, 400))

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

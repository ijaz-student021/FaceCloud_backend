import tempfile
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import FaceValidationResult
from .services import _detect, validate_image_faces

User = get_user_model()


class FaceValidationTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media.name)
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

    def test_bundled_detector_asset_loads(self):
        boxes, confidences = _detect(np.full((120, 120), 128, dtype=np.uint8))
        self.assertEqual(len(boxes), len(confidences))

    @patch("validation.services._detect")
    def test_one_suitable_face_is_accepted(self, detector):
        detector.return_value = (np.array([[100, 80, 180, 180]]), [0.92])
        result = validate_image_faces(self.image)
        self.assertEqual(result.decision, "accepted")
        self.assertEqual(result.face_count, 1)
        self.assertEqual(result.rejection_reason, "")

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

import tempfile
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ImageQualityDecision
from .services import calculate_scores, make_decision, process_quality_decision

User = get_user_model()


GOOD_METRICS = {
    "width": 1200, "height": 900, "mean_luminance": 135.0,
    "contrast_stddev": 55.0, "laplacian_sharpness": 180.0,
    "estimated_noise": 2.0, "shadow_clipping_percent": 1.0,
    "highlight_clipping_percent": 1.0, "defects": [],
    "needs_ai_restoration": False,
}


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class QualityDecisionTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.override = override_settings(
            MEDIA_ROOT=self.media.name,
            CELERY_TASK_ALWAYS_EAGER=True,
            CELERY_TASK_EAGER_PROPAGATES=True,
        )
        self.override.enable()
        self.user = User.objects.create_user(
            username="quality-owner", email="quality-owner@example.com",
            password="StrongPass!4729", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="quality.jpg", status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=self.image, status="completed",
            output_name="quality.jpg", output_format="jpeg",
        )
        ok, encoded = cv2.imencode(".jpg", np.full((600, 800, 3), 128, dtype=np.uint8))
        self.assertTrue(ok)
        self.preprocessing.processed_file.save("quality.jpg", ContentFile(encoded.tobytes()))
        self.face = FaceValidationResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            decision="accepted", face_count=3, message="Valid group image.",
        )
        token = RefreshToken.for_user(self.user)
        token["role"] = self.user.role
        token["token_version"] = self.user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def tearDown(self):
        self.override.disable()
        self.media.cleanup()

    def create_decision(self):
        return ImageQualityDecision.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            face_validation=self.face, status="queued",
        )

    def test_good_group_image_is_accepted(self):
        scores = calculate_scores(GOOD_METRICS)
        decision, reasons, _message = make_decision(GOOD_METRICS, scores, self.face)
        self.assertEqual(decision, "accepted")
        self.assertEqual(reasons, [])
        self.assertGreaterEqual(scores["overall_score"], 60)

    def test_severe_blur_and_low_resolution_are_rejected_when_there_is_no_face_to_work_with(self):
        metrics = {**GOOD_METRICS, "width": 180, "height": 180, "laplacian_sharpness": 5.0}
        decision, reasons, _message = make_decision(metrics, calculate_scores(metrics), None)
        self.assertEqual(decision, "rejected")
        self.assertIn("severe_blur", reasons)
        self.assertIn("resolution_too_low", reasons)

    def test_with_a_face_blur_never_rejects_but_a_too_small_image_still_does(self):
        metrics = {**GOOD_METRICS, "width": 180, "height": 180, "laplacian_sharpness": 5.0}
        decision, reasons, _message = make_decision(metrics, calculate_scores(metrics), self.face)
        self.assertEqual(decision, "rejected")
        self.assertEqual(reasons, ["resolution_too_low"])

    def test_uncertain_face_or_borderline_quality_needs_review(self):
        self.face.decision = "needs_review"
        self.face.save(update_fields=["decision"])
        metrics = {**GOOD_METRICS, "laplacian_sharpness": 45.0}
        decision, reasons, _message = make_decision(metrics, calculate_scores(metrics), self.face)
        self.assertEqual(decision, "needs_review")
        self.assertIn("face_validation_required", reasons)
        self.assertNotIn("borderline_blur", reasons)   # a face was found: blur alone never asks for review

    @patch("quality_control.services.analyze_image_quality", return_value=GOOD_METRICS)
    def test_processing_persists_scores_and_is_idempotent(self, _analysis):
        result = process_quality_decision(self.create_decision().pk)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.decision, "accepted")
        self.assertIsNotNone(result.overall_score)
        rerun = process_quality_decision(result.pk)
        self.assertEqual(rerun.pk, result.pk)

    def test_api_queues_analysis_and_is_owner_scoped(self):
        response = self.client.post(
            reverse("quality:image-quality", kwargs={"image_id": self.image.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "queued")

        other = User.objects.create_user(
            username="quality-other", email="quality-other@example.com",
            password="StrongPass!4729", is_email_verified=True,
        )
        token = RefreshToken.for_user(other)
        token["role"] = other.role
        token["token_version"] = other.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        hidden = self.client.get(
            reverse("quality:image-quality", kwargs={"image_id": self.image.pk})
        )
        self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)

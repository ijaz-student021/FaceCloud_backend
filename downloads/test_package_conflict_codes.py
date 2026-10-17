"""When a package cannot be made yet, the 409 says why in a way a client can act on."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from enhancement.models import ImageEnhancementResult
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

User = get_user_model()


class PackageConflictCodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="pkg-codes", email="pkg-codes@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.batch = UploadBatch.objects.create(
            owner=self.user, upload_type="multiple", status="uploaded", processing_status="completed", total_files=2,
        )

    def photo(self, name, decision="accepted", enhanced=False):
        image = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name=name, status="uploaded")
        pre = ImagePreprocessingResult.objects.create(owner=self.user, image=image, status="completed")
        ImageQualityDecision.objects.create(
            owner=self.user, image=image, preprocessing_result=pre, status="completed", decision=decision,
            overall_score=80, brightness_score=75, sharpness_score=70,
        )
        if enhanced:
            ImageEnhancementResult.objects.create(
                owner=self.user, image=image, preprocessing_result=pre, requested_level="auto",
                applied_level="medium", status="completed",
            )
        return image

    def ask(self, package_type="accepted_results", batch=None):
        return self.client.post(
            reverse("downloads:package-create", args=[(batch or self.batch).pk]),
            {"package_type": package_type}, format="json",
        )

    def test_accepted_photos_that_were_never_enhanced_say_enhancement_is_required(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        response = self.ask()
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "enhancement_required")
        self.assertEqual(body["accepted_images"], 2)
        self.assertIn("not been enhanced", body["detail"])

    def test_only_rejected_photos_say_there_is_nothing_to_package(self):
        self.photo("bad.jpg", decision="rejected")
        response = self.ask()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_accepted_images")
        self.assertEqual(response.json()["detail"], "The batch has no accepted enhanced images ready for download.")

    def test_a_batch_still_processing_is_its_own_code(self):
        self.photo("a.jpg")
        UploadBatch.objects.filter(pk=self.batch.pk).update(processing_status="processing")
        for package_type in ("accepted_results", "complete_batch"):
            response = self.ask(package_type)
            self.assertEqual((response.status_code, response.json()["code"]), (409, "batch_still_processing"))

    def test_an_empty_batch_is_its_own_code(self):
        response = self.ask("complete_batch")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "batch_empty"))

    def test_after_enhancement_the_same_request_succeeds(self):
        image = self.photo("a.jpg")
        self.assertEqual(self.ask().status_code, 409)
        ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=image.preprocessing_result, requested_level="auto",
            applied_level="medium", status="completed",
        )
        from unittest.mock import patch
        with patch("downloads.tasks.generate_download_package_task.apply_async"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.ask()
        self.assertEqual(response.status_code, 202)

    def test_another_users_batch_is_still_404(self):
        other = User.objects.create_user(
            username="pkg-other", email="pkg-other@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        theirs = UploadBatch.objects.create(owner=other, upload_type="multiple", processing_status="completed")
        self.assertEqual(self.ask(batch=theirs).status_code, 404)

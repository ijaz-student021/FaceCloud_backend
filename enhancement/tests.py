import tempfile
from unittest.mock import Mock, patch

import cv2
import numpy as np
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import EnhancementBatchJob, EnhancementLevel, ImageEnhancementResult
from .services import (
    analyze_image_quality,
    compare_image_quality,
    enhance_bgr,
    process_enhancement_result,
    restore_low_resolution,
)
from .tasks import process_enhancement_image

User = get_user_model()


class EnhancementAppTests(SimpleTestCase):
    def test_enhancement_app_is_registered(self):
        config = apps.get_app_config("enhancement")

        self.assertEqual(config.name, "enhancement")
        self.assertEqual(config.verbose_name, "Image Enhancement")


class EnhancementModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="enhancement-owner",
            email="enhancement-owner@example.com",
            password="ForestRiver!4821",
            is_email_verified=True,
        )
        self.upload_batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.user,
            batch=self.upload_batch,
            original_name="portrait.png",
            status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user,
            image=self.image,
            status="completed",
            output_format="png",
        )

    def test_batch_job_defaults_and_progress(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user,
            source_batch=self.upload_batch,
            total_images=4,
            processed_images=3,
        )

        self.assertEqual(job.requested_level, EnhancementLevel.AUTO)
        self.assertEqual(job.progress_percentage, 75)

    def test_only_one_active_enhancement_per_image(self):
        ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.MEDIUM,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ImageEnhancementResult.objects.create(
                owner=self.user,
                image=self.image,
                preprocessing_result=self.preprocessing,
                requested_level=EnhancementLevel.HIGH,
            )

    def test_completed_result_allows_a_new_enhancement(self):
        ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.LOW,
            applied_level=EnhancementLevel.LOW,
            status="completed",
        )

        second = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.HIGH,
        )

        self.assertEqual(second.status, "pending")


class OpenCVPolicyTests(SimpleTestCase):
    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_auto_selects_high_for_dark_image_and_improves_luminance(self):
        source = np.full((80, 80, 3), 35, dtype=np.uint8)
        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertGreater(float(np.mean(enhanced)), float(np.mean(source)))
        self.assertEqual(parameters["colour_policy"], "preserve_lab_chroma_channels")

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True)
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_dark_image_routes_through_genuine_zero_dce_provider(self, zero_dce):
        source = np.full((80, 80, 3), 35, dtype=np.uint8)
        zero_dce.return_value = (
            np.full_like(source, 95),
            {"provider": "official_zero_dce_onnx_opencv_dnn", "curve_iterations": 8},
        )

        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertTrue(parameters["zero_dce"]["applied"])
        self.assertEqual(parameters["algorithm"], "zero_dce_hybrid_v1")
        self.assertEqual(parameters["colour_policy"], "learned_rgb_curves")
        self.assertGreater(float(enhanced.mean()), float(source.mean()))

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True)
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_zero_dce_failure_uses_auditable_opencv_fallback(self, zero_dce):
        from enhancement.zero_dce import ZeroDCEUnavailable

        zero_dce.side_effect = ZeroDCEUnavailable("model missing")
        source = np.full((80, 80, 3), 35, dtype=np.uint8)

        enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertFalse(parameters["zero_dce"]["applied"])
        self.assertEqual(parameters["zero_dce"]["fallback_reason"], "model missing")
        self.assertEqual(parameters["algorithm"], "opencv_adaptive_restoration_router_v2")
        self.assertGreater(float(enhanced.mean()), float(source.mean()))

    def test_well_lit_image_uses_low_policy(self):
        source = np.full((40, 40, 3), 190, dtype=np.uint8)
        _, level, _ = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.LOW)

    def test_quality_router_identifies_blur_and_low_resolution(self):
        source = np.full((200, 200, 3), 90, dtype=np.uint8)
        quality = analyze_image_quality(source)

        self.assertIn("severe_blur", quality["defects"])
        self.assertIn("low_resolution", quality["defects"])
        self.assertTrue(quality["needs_ai_restoration"])

    def test_high_policy_uses_quality_guard_metadata(self):
        source = np.full((80, 80, 3), 210, dtype=np.uint8)
        _, level, parameters = enhance_bgr(source, EnhancementLevel.HIGH)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertIn("quality_before", parameters)
        self.assertIn("quality_after", parameters)
        self.assertIn("quality_guard", parameters)

    def test_quality_comparison_normalizes_output_to_source_dimensions(self):
        source = np.zeros((90, 120, 3), dtype=np.uint8)
        cv2.rectangle(source, (20, 20), (100, 70), (220, 220, 220), 2)
        enlarged = cv2.resize(source, (240, 180), interpolation=cv2.INTER_CUBIC)

        comparison = compare_image_quality(source, enlarged)

        self.assertEqual(comparison["method"], "source_resolution_normalized_v1")
        self.assertEqual(comparison["reference_dimensions"], [120, 90])
        self.assertEqual(comparison["before"]["width"], 120)
        self.assertEqual(comparison["after"]["width"], 120)
        self.assertIn("laplacian_sharpness", comparison["deltas"])

    @override_settings(AI_RESTORATION_ENABLED=True)
    @patch("enhancement.services._load_edsr_model")
    def test_low_resolution_image_is_restored_at_two_times_scale(self, load_model):
        source = np.full((120, 100, 3), 90, dtype=np.uint8)
        model = Mock()
        model.upsample.return_value = cv2.resize(
            source, (200, 240), interpolation=cv2.INTER_CUBIC
        )
        load_model.return_value = model

        restored, metadata = restore_low_resolution(
            source, analyze_image_quality(source)
        )

        self.assertTrue(metadata["requested"])
        self.assertTrue(metadata["applied"])
        self.assertEqual(restored.shape[:2], (240, 200))
        self.assertEqual(metadata["input_dimensions"], [100, 120])
        self.assertEqual(metadata["output_dimensions"], [200, 240])

    @override_settings(AI_RESTORATION_ENABLED=True)
    @patch("enhancement.services._load_edsr_model")
    def test_restoration_failure_falls_back_without_breaking_enhancement(
        self, load_model
    ):
        load_model.side_effect = RuntimeError("EDSR runtime unavailable")
        source = np.full((120, 100, 3), 90, dtype=np.uint8)

        enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(enhanced.shape, source.shape)
        self.assertFalse(parameters["restoration"]["applied"])
        self.assertEqual(
            parameters["restoration"]["fallback_reason"],
            "EDSR runtime unavailable",
        )
        self.assertTrue(parameters["warnings"])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), AI_RESTORATION_ENABLED=False)
class EnhancementProcessingAndAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="opencv-owner", email="opencv@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="opencv-other", email="opencv-other@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="dark.jpg", status="uploaded",
        )
        success, encoded = cv2.imencode(".jpg", np.full((64, 64, 3), 40, dtype=np.uint8))
        self.assertTrue(success)
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=self.image, status="completed", output_format="jpeg",
            output_name="dark.jpg", output_width=64, output_height=64,
        )
        self.preprocessing.processed_file.save("dark.jpg", ContentFile(encoded.tobytes()))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_processing_creates_separate_enhanced_output(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.applied_level, EnhancementLevel.HIGH)
        self.assertTrue(result.enhanced_file.name)
        self.assertNotEqual(result.enhanced_file.name, self.preprocessing.processed_file.name)
        self.assertGreater(result.output_file_size, 0)

    @patch("enhancement.views.process_enhancement_image.apply_async")
    def test_owner_can_queue_enhancement_and_other_user_cannot_read_or_download(
        self, apply_async
    ):
        create = self.client.post(
            reverse("enhancement:image-enhance", kwargs={"image_id": self.image.pk}),
            {"level": "medium"}, format="json",
        )
        self.assertEqual(create.status_code, 202)
        result_id = create.data["id"]
        self.assertEqual(create.data["status"], "queued")
        self.assertTrue(create.data["task_id"])
        self.assertIsNone(create.data["download_url"])
        apply_async.assert_called_once()

        task_result = process_enhancement_image.run(result_id)
        self.assertEqual(task_result["status"], "completed")

        self.client.force_authenticate(self.other)
        detail = self.client.get(reverse("enhancement:result-detail", kwargs={"pk": result_id}))
        download = self.client.get(reverse("enhancement:result-download", kwargs={"pk": result_id}))
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(download.status_code, 404)

    def test_batch_api_processes_ready_images_and_reports_progress(self):
        response = self.client.post(
            reverse("enhancement:batch-enhance", kwargs={"batch_id": self.batch.pk}),
            {"level": "auto"}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "completed")
        self.assertEqual(response.data["total_images"], 1)
        self.assertEqual(response.data["successful_images"], 1)
        self.assertEqual(response.data["progress_percentage"], 100.0)

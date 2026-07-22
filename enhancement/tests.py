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

from .models import (
    EditMode,
    EnhancementBatchJob,
    EnhancementLevel,
    ImageEditVersion,
    ImageEnhancementResult,
)
from .services import (
    apply_gallery_adjustments,
    analyze_image_quality,
    compare_image_quality,
    enhance_bgr,
    process_enhancement_result,
    process_image_edit,
    restore_low_resolution,
)
from .tasks import process_enhancement_image, process_gallery_edit
from .zero_dce import _apply_curves_tiled

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
    def test_zero_curve_tiles_preserve_full_pixel_grid_without_seams(self):
        source = np.random.default_rng(2026).integers(
            0, 256, size=(260, 270, 3), dtype=np.uint8
        )
        curves = np.zeros((1, 24, 32, 32), dtype=np.float32)

        output = _apply_curves_tiled(source, curves, tile_edge=128)

        self.assertEqual(output.shape, source.shape)
        np.testing.assert_array_equal(output, source)

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
    def test_learned_model_cannot_over_brighten_or_shift_source_chroma(
        self, zero_dce
    ):
        source = np.zeros((96, 96, 3), dtype=np.uint8)
        source[:, :] = (35, 55, 85)
        zero_dce.return_value = (
            np.full_like(source, (245, 180, 230)),
            {"provider": "private_facecloud_curve", "curve_iterations": 8},
        )

        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.MEDIUM)

        before = parameters["quality_before"]
        after = parameters["quality_after"]
        guard = parameters["quality_guard"]
        self.assertEqual(level, EnhancementLevel.MEDIUM)
        self.assertLessEqual(
            after["mean_luminance"] - before["mean_luminance"], 18.5
        )
        self.assertLessEqual(guard["mean_chroma_shift"], 3.5)
        self.assertIn("luminance_gain_limited", guard["reasons"])
        self.assertEqual(
            parameters["zero_dce"]["chroma_protection"],
            "preserve_source_lab_chroma",
        )

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True)
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_learned_model_guard_preserves_source_contrast(self, zero_dce):
        source = np.full((96, 96, 3), 40, dtype=np.uint8)
        source[:, 48:] = 100
        zero_dce.return_value = (
            np.full_like(source, 180),
            {"provider": "private_facecloud_curve", "curve_iterations": 8},
        )

        _enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.HIGH)

        before = parameters["quality_before"]["contrast_stddev"]
        after = parameters["quality_after"]["contrast_stddev"]
        self.assertGreaterEqual(after, before * 0.75)
        self.assertIn("contrast_preserved", parameters["quality_guard"]["reasons"])

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_overexposed_input_is_not_brightened(self):
        source = np.full((96, 96, 3), 225, dtype=np.uint8)

        enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(parameters["exposure_mode"], "darken")
        self.assertLessEqual(float(enhanced.mean()), float(source.mean()))

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
        self.assertEqual(
            parameters["quality_guard"]["policy"],
            "adaptive_natural_output_guard_v2",
        )

    def test_gallery_manual_controls_preserve_dimensions_and_limit_clipping(self):
        source = np.zeros((120, 180, 3), dtype=np.uint8)
        source[:, :90] = (35, 55, 75)
        source[:, 90:] = (200, 215, 230)

        rendered, metadata = apply_gallery_adjustments(
            source,
            EditMode.MANUAL,
            {
                "brightness": 100,
                "contrast": 50,
                "shadows": 35,
                "saturation": 25,
                "highlights": 60,
            },
        )

        self.assertEqual(rendered.shape, source.shape)
        self.assertTrue(metadata["dimensions_preserved"])
        self.assertLessEqual(
            metadata["quality_after"]["highlight_clipping_percent"],
            max(
                5.0,
                metadata["quality_before"]["highlight_clipping_percent"] + 2.0,
            ),
        )

    def test_gallery_auto_mode_is_scene_aware_and_conservative(self):
        source = np.full((100, 100, 3), 210, dtype=np.uint8)

        rendered, metadata = apply_gallery_adjustments(source, EditMode.AUTO)

        self.assertEqual(rendered.shape, source.shape)
        self.assertLessEqual(abs(float(rendered.mean()) - float(source.mean())), 20)
        self.assertLessEqual(metadata["requested"]["brightness"], 0)

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
        success, encoded = cv2.imencode(".jpg", np.full((64, 64, 3), 40, dtype=np.uint8))
        self.assertTrue(success)
        self.image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="dark.jpg",
            file_extension=".jpg", mime_type="image/jpeg", file_size=len(encoded.tobytes()),
            width=64, height=64, status="uploaded",
        )
        self.image.original_file.save("dark.jpg", ContentFile(encoded.tobytes()))
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=self.image, status="completed", output_format="jpeg",
            output_name="dark.jpg", output_width=64, output_height=64,
            processed_file_size=len(encoded.tobytes()),
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

    def test_enhancement_uses_original_pixel_grid_instead_of_small_proxy(self):
        success, full = cv2.imencode(
            ".jpg", np.full((96, 160, 3), 90, dtype=np.uint8)
        )
        self.assertTrue(success)
        self.image.original_file.delete(save=False)
        self.image.original_file.save("full.jpg", ContentFile(full.tobytes()))
        self.image.width = 160
        self.image.height = 96
        self.image.save(update_fields=["width", "height", "updated_at"])
        success, proxy = cv2.imencode(
            ".jpg", np.full((32, 48, 3), 90, dtype=np.uint8)
        )
        self.assertTrue(success)
        self.preprocessing.processed_file.delete(save=False)
        self.preprocessing.processed_file.save(
            "proxy.jpg", ContentFile(proxy.tobytes())
        )
        self.preprocessing.output_width = 48
        self.preprocessing.output_height = 32
        self.preprocessing.save(
            update_fields=["output_width", "output_height", "updated_at"]
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.LOW,
        )

        result = process_enhancement_result(result)

        self.assertEqual((result.output_width, result.output_height), (160, 96))
        self.assertEqual(
            result.applied_parameters["source_resolution_strategy"],
            "full_resolution_original",
        )

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

    def test_owner_receives_normalized_comparison_contract(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)

        response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": result.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result_id"], result.pk)
        self.assertEqual(response.data["image_id"], self.image.pk)
        self.assertEqual(response.data["status"], "completed")
        self.assertTrue(response.data["algorithm"])
        self.assertEqual(
            response.data["comparison"]["method"],
            "source_resolution_normalized_v1",
        )
        self.assertEqual(
            response.data["comparison"]["reference_dimensions"], [64, 64]
        )
        self.assertEqual(
            set(response.data["comparison"]["metrics"]),
            {
                "brightness", "contrast", "sharpness", "noise",
                "shadow_clipping", "highlight_clipping",
            },
        )
        self.assertIn("/api/images/", response.data["original"]["download_url"])
        self.assertIn("/preprocessing/download/", response.data["standardized"]["download_url"])
        self.assertIn("/api/enhancements/results/", response.data["enhanced"]["download_url"])
        self.assertNotIn("users/", str(response.data))

    @patch("enhancement.views.process_gallery_edit.apply_async")
    def test_owner_can_render_versioned_gallery_edit_and_other_user_cannot_access(
        self, apply_async
    ):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)
        create = self.client.post(
            reverse(
                "enhancement:edit-list-create", kwargs={"result_id": result.pk}
            ),
            {
                "mode": "manual",
                "adjustments": {
                    "brightness": 12,
                    "contrast": 8,
                    "shadows": 10,
                    "saturation": 5,
                    "highlights": -6,
                },
            },
            format="json",
        )
        self.assertEqual(create.status_code, 202)
        self.assertEqual(create.data["status"], "queued")
        edit_id = create.data["id"]
        apply_async.assert_called_once()

        task_result = process_gallery_edit.run(edit_id)
        self.assertEqual(task_result["status"], "completed")
        edit = ImageEditVersion.objects.get(pk=edit_id)
        self.assertTrue(edit.is_current)
        self.assertEqual(
            (edit.output_width, edit.output_height),
            (result.output_width, result.output_height),
        )
        self.assertTrue(edit.edited_file)

        self.client.force_authenticate(self.other)
        self.assertEqual(
            self.client.get(
                reverse("enhancement:edit-detail", kwargs={"pk": edit_id})
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("enhancement:edit-download", kwargs={"pk": edit_id})
            ).status_code,
            404,
        )

    def test_editor_configuration_exposes_auto_and_five_manual_controls(self):
        response = self.client.get(reverse("enhancement:editor-configuration"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["non_destructive"])
        self.assertEqual(response.data["modes"], ["auto", "manual"])
        self.assertEqual(
            set(response.data["controls"]),
            {"brightness", "contrast", "shadows", "saturation", "highlights"},
        )

    def test_comparison_is_private_and_requires_authentication(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)
        url = reverse("enhancement:result-comparison", kwargs={"pk": result.pk})

        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_comparison_rejects_incomplete_and_missing_file_results(self):
        pending = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        pending_response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": pending.pk})
        )
        self.assertEqual(pending_response.status_code, 409)
        self.assertEqual(pending_response.data["status"], "pending")

        pending.status = "failed"
        pending.failure_reason = "Safe processing failure."
        pending.save(update_fields=["status", "failure_reason", "updated_at"])
        failed_response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": pending.pk})
        )
        self.assertEqual(failed_response.status_code, 409)
        self.assertEqual(failed_response.data["status"], "failed")
        self.assertEqual(
            failed_response.data["failure_reason"], "Safe processing failure."
        )

        pending.status = "completed"
        pending.applied_parameters = {
            "quality_comparison": {"method": "source_resolution_normalized_v1"}
        }
        pending.save(update_fields=["status", "applied_parameters", "updated_at"])
        missing_response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": pending.pk})
        )
        self.assertEqual(missing_response.status_code, 410)

    def test_comparison_requires_normalized_measurements(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)
        result.applied_parameters = {}
        result.save(update_fields=["applied_parameters", "updated_at"])

        response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": result.pk})
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("measurements", response.data["detail"].lower())

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

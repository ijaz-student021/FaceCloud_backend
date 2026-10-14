"""Regression tests for confirmed enhancement-pipeline and task defects.

Every model here is mocked: these tests prove how the pipeline routes, guards and
records candidates, not what a real checkpoint does to a photograph.
"""
from unittest.mock import Mock, patch

import cv2
import numpy as np
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db.utils import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .hd_restoration import HDRestorationUnavailable
from .models import EnhancementBatchJob, EnhancementLevel, ImageEnhancementResult
from .services import (
    TIME_LIMIT_FAILURE_REASON,
    EnhancementPipelineContext,
    _fit_pixel_budget,
    analyze_image_quality,
    enhance_bgr,
    hd_restoration_stage,
    output_guard_b_stage,
    process_enhancement_result,
)
from .tasks import process_enhancement_batch, process_enhancement_image

User = get_user_model()

NO_MODELS = dict(
    HD_RESTORATION_ENABLED=False, NAFNET_DEBLUR_ENABLED=False, PURESCALE_ENABLED=False,
    PUREVISION_ENABLED=False, GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    COLORIZATION_ENABLED=False, ZERO_DCE_ENABLED=False, IDENTITY_GUARD_ENABLED=False,
)


def textured(height=64, width=96, base=110):
    """A mid-tone photo-like array with gradients and colour, not a flat fill."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    luminance = base + 40 * np.sin(xx / width * 6.0) + 25 * np.cos(yy / height * 4.0)
    image = np.stack([luminance * 0.85, luminance, luminance * 1.1], axis=2)
    return np.clip(image, 0, 255).astype(np.uint8)


class DeblurSurvivesFailedUpscaleTests(SimpleTestCase):
    @override_settings(**{**NO_MODELS, "HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True})
    @patch("enhancement.services.upscale_with_realesrgan")
    @patch("enhancement.services.deblur_with_nafnet")
    def test_stage_keeps_the_deblurred_image_when_the_upscaler_fails(self, deblur, upscale):
        source = textured()
        deblurred = cv2.add(source, 7)
        deblur.return_value = (deblurred, {"provider": "nafnet_gopro_width64"})
        upscale.side_effect = HDRestorationUnavailable("The Real-ESRGAN checkpoint is unavailable.")
        context = EnhancementPipelineContext(source, EnhancementLevel.LOW)

        hd_restoration_stage(context)

        np.testing.assert_array_equal(context.current_image, deblurred)
        self.assertTrue(context.hd_restoration["deblur"]["applied"])
        self.assertFalse(context.hd_restoration["applied"])
        self.assertIn("unavailable", context.hd_restoration["fallback_reason"])

    @override_settings(**{**NO_MODELS, "HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True})
    @patch("enhancement.services.upscale_with_realesrgan")
    @patch("enhancement.services.deblur_with_nafnet")
    def test_pipeline_reports_the_deblur_without_claiming_an_upscale(self, deblur, upscale):
        source = textured()
        deblur.side_effect = lambda image: (cv2.add(image, 7), {"provider": "nafnet_gopro_width64"})
        upscale.side_effect = HDRestorationUnavailable("The native 4x output exceeds the configured safety limit.")

        output, _level, parameters = enhance_bgr(source, "low")

        self.assertEqual(output.shape, source.shape)
        self.assertIn("nafnet_gopro", parameters["algorithm"])
        self.assertNotIn("realesrgan", parameters["algorithm"])
        self.assertEqual(parameters["resolution_contract"]["pixel_grid_source"], "original_pixel_grid")

    @override_settings(**{**NO_MODELS, "HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True})
    @patch("enhancement.services.upscale_with_realesrgan")
    @patch("enhancement.services.deblur_with_nafnet")
    def test_both_failing_leaves_the_image_untouched(self, deblur, upscale):
        source = textured()
        deblur.side_effect = HDRestorationUnavailable("The NAFNet candidate failed the image-fidelity guard.")
        upscale.side_effect = HDRestorationUnavailable("The Real-ESRGAN checkpoint is unavailable.")
        context = EnhancementPipelineContext(source, EnhancementLevel.LOW)

        hd_restoration_stage(context)

        np.testing.assert_array_equal(context.current_image, source)


class SingleUpscalerTests(SimpleTestCase):
    @override_settings(**{**NO_MODELS, "AI_RESTORATION_ENABLED": True, "PURESCALE_ENABLED": True})
    @patch("enhancement.services.upscale_with_purescale")
    @patch("enhancement.services._load_edsr_model")
    def test_purescale_does_not_stack_on_edsr(self, load_edsr, purescale):
        edsr = Mock()
        edsr.upsample.side_effect = lambda image: cv2.resize(
            image, (image.shape[1] * 2, image.shape[0] * 2), interpolation=cv2.INTER_CUBIC,
        )
        load_edsr.return_value = edsr
        source = textured()  # 96x64: low resolution, so EDSR is requested

        output, _level, parameters = enhance_bgr(source, "low")

        self.assertTrue(parameters["restoration"]["applied"])
        self.assertEqual(output.shape[:2], (128, 192))  # 2x once, not 4x
        purescale.assert_not_called()
        self.assertFalse(parameters["purescale"]["applied"])
        self.assertIn("EDSR already upscaled", parameters["purescale"]["fallback_reason"])
        self.assertEqual(parameters["resolution_contract"]["pixel_grid_source"], "bounded_edsr_output_grid")

    @override_settings(**{**NO_MODELS, "AI_RESTORATION_ENABLED": True, "PURESCALE_ENABLED": True})
    @patch("enhancement.services.upscale_with_purescale")
    @patch("enhancement.services._load_edsr_model", side_effect=RuntimeError("The configured EDSR model file is unavailable."))
    def test_purescale_still_runs_when_edsr_was_unavailable(self, _load_edsr, purescale):
        purescale.side_effect = lambda image: (
            cv2.resize(image, (image.shape[1] * 2, image.shape[0] * 2), interpolation=cv2.INTER_CUBIC),
            {"provider": "purescale_safmn_2x"},
        )
        output, _level, parameters = enhance_bgr(textured(), "low")

        self.assertFalse(parameters["restoration"]["applied"])
        self.assertTrue(parameters["purescale"]["applied"])
        self.assertEqual(output.shape[:2], (128, 192))


class FinalMetricsTests(SimpleTestCase):
    @override_settings(**NO_MODELS)
    def test_quality_after_measures_the_styled_pixels_that_are_saved(self):
        output, _level, parameters = enhance_bgr(textured(), "low", style="dynamic")

        self.assertTrue(parameters["dynamic_style"]["applied"])
        self.assertEqual(parameters["quality_after"], analyze_image_quality(output))
        self.assertEqual(parameters["quality_after_measurement_point"], "final_output")

    @override_settings(**{**NO_MODELS, "PURESCALE_ENABLED": True})
    @patch("enhancement.services.upscale_with_purescale")
    def test_quality_after_measures_the_upscaled_output_grid(self, purescale):
        purescale.side_effect = lambda image: (
            cv2.resize(image, (image.shape[1] * 2, image.shape[0] * 2), interpolation=cv2.INTER_CUBIC),
            {"provider": "purescale_safmn_2x"},
        )
        output, _level, parameters = enhance_bgr(textured(), "low", style="dynamic")

        self.assertEqual((parameters["quality_after"]["width"], parameters["quality_after"]["height"]), (192, 128))
        self.assertEqual(parameters["quality_after"], analyze_image_quality(output))


class GuardReferenceTests(SimpleTestCase):
    @override_settings(**NO_MODELS)
    def test_guard_b_judges_an_upscaled_image_against_the_pre_restoration_reference(self):
        accepted = textured()
        upscaled = cv2.resize(accepted, (192, 128), interpolation=cv2.INTER_CUBIC)
        # A restoration model that shifted colour while it upscaled.
        cast = upscaled.copy()
        cast[:, :, 2] = np.clip(cast[:, :, 2].astype(np.int16) + 45, 0, 255).astype(np.uint8)
        context = EnhancementPipelineContext(accepted, EnhancementLevel.LOW)
        context.applied_level = EnhancementLevel.LOW
        context.restoration_reference = accepted
        context.current_image = cast

        output_guard_b_stage(context)

        guard = context.quality_guards["guard_b"]
        self.assertEqual(guard["reference"]["basis"], "pre_restoration_output")
        self.assertTrue(guard["reference"]["resized_for_comparison"])
        self.assertIn("chroma_shift_limited", guard["reasons"])
        self.assertTrue(guard["fallback_blend_applied"])
        self.assertLessEqual(guard["mean_chroma_shift"], guard["limits"]["max_chroma_shift"])
        self.assertEqual(context.current_image.shape[:2], (128, 192))

    @override_settings(**{**NO_MODELS, "PURESCALE_ENABLED": True})
    @patch("enhancement.services.upscale_with_purescale")
    def test_an_upscaler_that_brightens_is_pulled_back_in_the_pipeline(self, purescale):
        def brightening_upscaler(image):
            doubled = cv2.resize(image, (image.shape[1] * 2, image.shape[0] * 2), interpolation=cv2.INTER_CUBIC)
            return cv2.add(doubled, 60), {"provider": "purescale_safmn_2x"}

        purescale.side_effect = brightening_upscaler
        source = textured(base=120)

        output, _level, parameters = enhance_bgr(source, "low")

        guard = parameters["quality_guards"]["guard_b"]
        self.assertIn("luminance_gain_limited", guard["reasons"])
        self.assertLessEqual(guard["luminance_gain"], guard["limits"]["max_luminance_gain"] + 0.5)
        self.assertLess(float(output.mean()) - float(source.mean()), 30.0)

    @override_settings(**{**NO_MODELS, "PURESCALE_ENABLED": True})
    @patch("enhancement.services.upscale_with_purescale")
    def test_a_faithful_upscaler_is_kept_at_full_strength(self, purescale):
        purescale.side_effect = lambda image: (
            cv2.resize(image, (image.shape[1] * 2, image.shape[0] * 2), interpolation=cv2.INTER_LANCZOS4),
            {"provider": "purescale_safmn_2x"},
        )
        _output, _level, parameters = enhance_bgr(textured(base=120), "low")

        self.assertFalse(parameters["quality_guards"]["guard_b"]["fallback_blend_applied"])


class PixelBudgetTests(SimpleTestCase):
    @override_settings(ENHANCEMENT_MAX_INPUT_PIXELS=24_000_000)
    def test_a_source_inside_the_budget_is_returned_untouched(self):
        source = textured(60, 80)
        fitted, metadata = _fit_pixel_budget(source)
        self.assertIs(fitted, source)
        self.assertFalse(metadata["reduced"])

    @override_settings(ENHANCEMENT_MAX_INPUT_PIXELS=1200)
    def test_an_oversized_source_is_reduced_to_the_budget_keeping_its_shape(self):
        fitted, metadata = _fit_pixel_budget(textured(60, 80))
        height, width = fitted.shape[:2]
        self.assertLessEqual(width * height, 1200)
        self.assertAlmostEqual(width / height, 80 / 60, delta=0.05)
        self.assertEqual(metadata["original_dimensions"], [80, 60])
        self.assertEqual(metadata["working_dimensions"], [width, height])

    @override_settings(ENHANCEMENT_MAX_INPUT_PIXELS=0)
    def test_zero_disables_the_budget(self):
        source = textured(60, 80)
        self.assertIs(_fit_pixel_budget(source)[0], source)


class ResultFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="regression", email="regression@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")

    def make_result(self, name="photo.jpg", size=(64, 96), job=None, status="queued"):
        encoded = cv2.imencode(".jpg", textured(*size))[1].tobytes()
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name=name, file_extension=".jpg",
            mime_type="image/jpeg", file_size=len(encoded), width=size[1], height=size[0], status="uploaded",
        )
        image.original_file.save(name, ContentFile(encoded))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="jpeg", output_name=name,
            output_width=size[1], output_height=size[0], processed_file_size=len(encoded),
        )
        preprocessing.processed_file.save(name, ContentFile(encoded))
        return ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=preprocessing,
            batch_job=job, status=status, requested_level=EnhancementLevel.LOW,
        )


@override_settings(**NO_MODELS)
class ProcessResultFailureTests(ResultFixture):
    @override_settings(ENHANCEMENT_MAX_INPUT_PIXELS=2000)
    def test_an_oversized_original_is_enhanced_at_the_budget_and_says_so(self):
        result = process_enhancement_result(self.make_result())

        self.assertEqual(result.status, "completed")
        self.assertLessEqual(result.output_width * result.output_height, 2000)
        parameters = result.applied_parameters
        self.assertEqual(parameters["source_resolution_strategy"], "original_reduced_to_pixel_budget")
        self.assertEqual(parameters["input_pixel_budget"]["original_dimensions"], [96, 64])
        self.assertTrue(any("pixel budget" in warning for warning in parameters["warnings"]))

    def test_an_out_of_memory_error_is_logged_and_recorded_as_a_failure(self):
        result = self.make_result()
        with patch("enhancement.services.enhance_bgr", side_effect=RuntimeError("CUDA out of memory")):
            with self.assertLogs("enhancement.services", level="ERROR") as logged:
                with self.assertRaises(ValidationError):
                    process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.finished_at)
        self.assertNotIn("CUDA", result.failure_reason)  # the user sees a safe sentence
        self.assertIn("CUDA out of memory", "\n".join(logged.output))  # the log keeps the cause

    def test_a_memory_error_does_not_strand_the_result(self):
        result = self.make_result()
        with patch("enhancement.services.enhance_bgr", side_effect=MemoryError()):
            with self.assertLogs("enhancement.services", level="ERROR"):
                with self.assertRaises(ValidationError):
                    process_enhancement_result(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")

    def test_a_failure_while_saving_removes_the_output_file_it_wrote(self):
        result = self.make_result()
        saved_names = []
        original_save = ImageEnhancementResult.save

        def failing_save(instance, *args, **kwargs):
            if instance.status == "completed":
                saved_names.append(instance.enhanced_file.name)
                raise ValueError("row could not be written")
            return original_save(instance, *args, **kwargs)

        with patch.object(ImageEnhancementResult, "save", failing_save):
            with self.assertLogs("enhancement.services", level="ERROR"):
                with self.assertRaises(ValidationError):
                    process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertTrue(saved_names)
        self.assertFalse(result.enhanced_file.storage.exists(saved_names[0]))

    def test_the_time_limit_is_recorded_and_still_reaches_the_task(self):
        result = self.make_result()
        with patch("enhancement.services.enhance_bgr", side_effect=SoftTimeLimitExceeded()):
            with self.assertRaises(SoftTimeLimitExceeded):
                process_enhancement_result(result)
        result.refresh_from_db()
        self.assertEqual((result.status, result.failure_reason), ("failed", TIME_LIMIT_FAILURE_REASON))


class SingleImageTaskTests(ResultFixture):
    def test_an_unexpected_error_is_logged_with_its_cause(self):
        result = self.make_result()
        with patch("enhancement.tasks.process_enhancement_result", side_effect=RuntimeError("CUDA out of memory")):
            with self.assertLogs("enhancement.tasks", level="ERROR") as logged:
                outcome = process_enhancement_image.apply(args=[result.pk]).get()

        result.refresh_from_db()
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(result.status, "failed")
        self.assertIn(f"result {result.pk}", logged.output[0])
        self.assertIn("CUDA out of memory", logged.output[0])

    def test_a_dropped_database_connection_is_retried_until_it_works(self):
        result = self.make_result()
        attempts = []

        def flaky(row, claim_owner=None, **kwargs):
            attempts.append(row.pk)
            if len(attempts) < 3:
                raise OperationalError("server closed the connection unexpectedly")
            row.status = "completed"
            row.save(update_fields=["status"])
            return row

        with patch("enhancement.tasks.process_enhancement_result", side_effect=flaky):
            process_enhancement_image.apply(args=[result.pk])

        result.refresh_from_db()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result.status, "completed")

    def test_retries_are_bounded_and_the_last_attempt_records_the_failure(self):
        result = self.make_result()
        attempts = []

        def always_down(row, claim_owner=None, **kwargs):
            attempts.append(row.pk)
            raise OperationalError("server closed the connection unexpectedly")

        with patch("enhancement.tasks.process_enhancement_result", side_effect=always_down):
            with self.assertLogs("enhancement.tasks", level="ERROR"):
                process_enhancement_image.apply(args=[result.pk])

        result.refresh_from_db()
        self.assertEqual(len(attempts), 1 + process_enhancement_image.max_retries)
        self.assertEqual(result.status, "failed")

    def test_the_time_limit_fails_the_result_with_a_reason_the_user_can_act_on(self):
        result = self.make_result()
        with patch("enhancement.tasks.process_enhancement_result", side_effect=SoftTimeLimitExceeded()):
            with self.assertLogs("enhancement.tasks", level="ERROR"):
                process_enhancement_image.apply(args=[result.pk])
        result.refresh_from_db()
        self.assertEqual((result.status, result.failure_reason), ("failed", TIME_LIMIT_FAILURE_REASON))


class BatchTimeLimitTests(ResultFixture):
    def test_the_batch_stops_at_the_time_limit_instead_of_starting_more_images(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=5,
        )
        results = [self.make_result(f"{index}.jpg", job=job) for index in range(5)]
        attempted = []

        def slow_second_image(row, claim_owner=None, **kwargs):
            attempted.append(row.pk)
            if len(attempted) == 2:
                raise SoftTimeLimitExceeded()
            row.status = "completed"
            row.save(update_fields=["status"])
            return row

        with patch("enhancement.tasks.process_enhancement_result", side_effect=slow_second_image):
            with self.assertLogs("enhancement.tasks", level="ERROR"):
                outcome = process_enhancement_batch.run(job.pk)

        self.assertEqual(attempted, [results[0].pk, results[1].pk])  # images 3-5 were never started
        statuses = [ImageEnhancementResult.objects.get(pk=row.pk) for row in results]
        self.assertEqual([row.status for row in statuses], ["completed", "failed", "failed", "failed", "failed"])
        self.assertTrue(all(row.failure_reason == TIME_LIMIT_FAILURE_REASON for row in statuses[1:]))
        job.refresh_from_db()
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual((job.successful_images, job.failed_images), (1, 4))
        self.assertIn("ran out of time", job.failure_reason)
        self.assertIsNotNone(job.finished_at)

    def test_a_dropped_connection_retries_the_batch_instead_of_failing_the_image(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=2,
        )
        results = [self.make_result(f"{index}.jpg", job=job) for index in range(2)]
        attempts = []

        def flaky_once(row, claim_owner=None, **kwargs):
            attempts.append(row.pk)
            if len(attempts) == 1:
                raise OperationalError("server closed the connection unexpectedly")
            row.status = "completed"
            row.save(update_fields=["status"])
            return row

        with patch("enhancement.tasks.process_enhancement_result", side_effect=flaky_once):
            process_enhancement_batch.apply(args=[job.pk])

        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            [ImageEnhancementResult.objects.get(pk=row.pk).status for row in results],
            ["completed", "completed"],
        )


class PreprocessingTimeLimitTests(ResultFixture):
    def test_preprocessing_stops_at_the_time_limit_and_reports_a_partial_batch(self):
        from uploads.tasks import preprocess_batch_task

        for index in range(3):
            ImageUpload.objects.create(
                owner=self.user, batch=self.batch, original_name=f"{index}.jpg", status="uploaded",
            )
        attempted = []

        def slow_second_image(image, requested_format):
            attempted.append(image.pk)
            if len(attempted) == 2:
                raise SoftTimeLimitExceeded()
            ImagePreprocessingResult.objects.create(owner=self.user, image=image, status="completed")

        with patch("uploads.tasks.preprocess_image", side_effect=slow_second_image):
            with self.assertLogs("uploads.tasks", level="ERROR"):
                outcome = preprocess_batch_task.run(self.batch.pk)

        self.assertEqual(len(attempted), 2)  # the third image was never started
        self.batch.refresh_from_db()
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(self.batch.processing_status, "partial")
        self.assertIn("ran out of time", self.batch.processing_error)

    def test_an_unexpected_preprocessing_error_is_logged_with_its_cause(self):
        from uploads.tasks import preprocess_batch_task

        ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name="a.jpg", status="uploaded")
        with patch("uploads.tasks.preprocess_image", side_effect=MemoryError("decoder ran out of memory")):
            with self.assertLogs("uploads.tasks", level="ERROR") as logged:
                outcome = preprocess_batch_task.run(self.batch.pk)

        self.assertEqual(outcome["status"], "failed")
        self.assertIn("decoder ran out of memory", logged.output[0])

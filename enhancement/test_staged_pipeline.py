"""Tests for the staged candidate pipeline: plan, stage reports, face checks,
identity status, ownership claims, cancellation and output verification.

These tests establish what the (classical) pipeline does with a candidate, a
failure or a refusal: its plan, stage reports, face checks, identity status,
claims, cancellation and output verification.
"""
import io
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult, ImageUpload

from . import face_fidelity
from .face_fidelity import check_face_regions
from .identity_guard import identity_status
from .models import EnhancementBatchJob, EnhancementLevel, ImageEnhancementResult
from .services import (
    EnhancementCancelled,
    EnhancementSuperseded,
    ResultInProgress,
    _verify_encoded_output,
    enhance_bgr,
    process_enhancement_result,
)
from .tasks import process_enhancement_batch, process_enhancement_image
from .test_pipeline_regressions import NO_MODELS, ResultFixture, textured

WHOLE_IMAGE = [(0.0, 0.0, 1.0, 1.0)]


def report(parameters, stage):
    return [entry for entry in parameters["stage_reports"] if entry["stage"] == stage][-1]


def skin(height=160, width=160, seed=5):
    """A face-sized patch with pore-like fine texture on a skin-toned gradient."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    base = 150 + 25 * np.sin(xx / width * 3.0) + 15 * np.cos(yy / height * 2.0)
    texture = cv2.GaussianBlur(rng.normal(0, 14, (height, width)).astype(np.float32), (0, 0), 0.8)
    luminance = base + texture
    return np.clip(np.stack([luminance * 0.72, luminance * 0.85, luminance], axis=2), 0, 255).astype(np.uint8)


class StageReportTests(SimpleTestCase):
    @override_settings(**NO_MODELS)
    def test_every_stage_reports_its_outcome_and_duration(self):
        _output, _level, parameters = enhance_bgr(textured(), "medium")

        stages = [entry["stage"] for entry in parameters["stage_reports"]]
        self.assertEqual(stages, parameters["stage_order"])
        for entry in parameters["stage_reports"]:
            self.assertIn("status", entry)
            self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertEqual(report(parameters, "exposure_enhancement")["status"], "accepted")
        self.assertEqual(report(parameters, "exposure_enhancement")["provider"], "opencv_lab_gamma_clahe")
        self.assertEqual(report(parameters, "exposure_enhancement")["device"], "cpu")
        self.assertEqual(report(parameters, "output_guard_b")["status"], "completed")
        self.assertEqual(parameters["pipeline_version"], "staged_candidate_pipeline_v5")

    @override_settings(**NO_MODELS)
    def test_the_source_array_is_never_modified(self):
        source = textured()
        untouched = source.copy()
        output, _level, _parameters = enhance_bgr(source, "high", style="dynamic")
        np.testing.assert_array_equal(source, untouched)
        self.assertIsNot(output, source)

    @override_settings(**{**NO_MODELS, "IDENTITY_GUARD_ENABLED": True})
    def test_an_unexpected_error_in_an_optional_stage_keeps_the_last_safe_image(self):
        source = textured()
        for error in (RuntimeError("unexpected tensor shape"), MemoryError()):
            with self.subTest(error=type(error).__name__), \
                    patch("enhancement.services.apply_identity_guard", side_effect=error), \
                    self.assertLogs("enhancement.services", level="ERROR") as logged:
                output, _level, parameters = enhance_bgr(source, "low")

            self.assertEqual(output.shape, source.shape)
            entry = report(parameters, "identity_guard")
            self.assertEqual(entry["status"], "failed")
            self.assertFalse(parameters["identity_guard"]["applied"])
            self.assertTrue(parameters["identity_guard"]["failed"])
            self.assertTrue(any("identity guard stage failed safely" in w for w in parameters["warnings"]))
            self.assertTrue(logged.output)
            self.assertEqual(parameters["resolution_contract"]["pixel_grid_source"], "original_pixel_grid")

    @override_settings(**NO_MODELS)
    @patch("enhancement.services._quality_guard", side_effect=cv2.error("guard could not run"))
    def test_a_guard_that_cannot_run_fails_the_enhancement_instead_of_passing_it(self, _guard):
        with self.assertRaises(cv2.error):
            enhance_bgr(textured(), "low")

class ProcessingPlanTests(SimpleTestCase):
    @override_settings(**NO_MODELS)
    def test_a_dark_noisy_image_is_planned_for_exposure_and_denoise(self):
        rng = np.random.default_rng(2)
        dark = np.clip(textured(720, 960, base=35).astype(np.int16) + rng.normal(0, 9, (720, 960, 3)), 0, 255).astype(np.uint8)
        _output, level, parameters = enhance_bgr(dark, "high")  # lifting a dark photo is asked for

        plan = parameters["plan"]
        self.assertEqual(level, "high")
        self.assertIn("severe_low_light", plan["defects"])
        self.assertFalse(plan["minimal_processing"])
        self.assertTrue(plan["steps"]["exposure"]["planned"])
        self.assertTrue(plan["steps"]["denoise"]["planned"])
        self.assertEqual(set(plan["steps"]), {"exposure", "denoise", "sharpen", "style"})

    @override_settings(**NO_MODELS)
    def test_an_already_good_image_gets_minimal_processing(self):
        good = textured(720, 960, base=135)
        output, level, parameters = enhance_bgr(good, "auto")

        self.assertEqual(level, "low")
        self.assertTrue(parameters["plan"]["minimal_processing"])
        self.assertFalse(parameters["adaptive_denoise"]["applied"])
        difference = np.abs(output.astype(np.int16) - good.astype(np.int16))
        self.assertLess(float(difference.mean()), 2.5)
        self.assertLessEqual(int(np.percentile(difference, 99)), 8)
        self.assertLessEqual(parameters["quality_comparison"]["deltas"]["highlight_clipping_percent"], 0.0)

    @override_settings(**NO_MODELS)
    def test_an_explicit_level_or_style_is_not_treated_as_minimal(self):
        good = textured(720, 960, base=135)
        self.assertFalse(enhance_bgr(good, "high")[2]["plan"]["minimal_processing"])
        self.assertFalse(enhance_bgr(good, "auto", style="dynamic")[2]["plan"]["minimal_processing"])

class IdentityStatusTests(SimpleTestCase):
    def test_guard_outcomes_map_onto_four_states(self):
        self.assertEqual(identity_status({"status": "preserved"}), "verified")
        self.assertEqual(identity_status({"status": "strength_reduced"}), "verified")
        self.assertEqual(identity_status({"status": "inconclusive"}), "inconclusive")
        self.assertEqual(identity_status({"status": "unresolved"}), "failed")
        self.assertEqual(identity_status({"status": "disabled"}), "unavailable")

    def test_a_skip_is_inconclusive_only_when_the_source_was_actually_examined(self):
        self.assertEqual(identity_status({"status": "skipped", "reference_checked": True}), "inconclusive")
        self.assertEqual(identity_status({"status": "skipped"}), "unavailable")

    @override_settings(**NO_MODELS)
    def test_the_pipeline_reports_unavailable_when_the_guard_is_off(self):
        self.assertEqual(enhance_bgr(textured(), "low")[2]["identity_status"], "unavailable")

    @override_settings(**{**NO_MODELS, "IDENTITY_GUARD_ENABLED": True})
    def test_the_pipeline_reports_unavailable_without_consent(self):
        parameters = enhance_bgr(textured(), "low", allow_biometric_processing=False)[2]
        self.assertEqual(parameters["identity_status"], "unavailable")

    @override_settings(**{**NO_MODELS, "IDENTITY_GUARD_ENABLED": True})
    @patch("enhancement.services.apply_identity_guard")
    def test_the_pipeline_reports_what_the_guard_found(self, guard):
        for status, expected in (("preserved", "verified"), ("inconclusive", "inconclusive"), ("unresolved", "failed")):
            guard.side_effect = lambda source, candidate, status=status: (
                candidate, {"status": status, "applied": False, "reference_checked": True},
            )
            self.assertEqual(enhance_bgr(textured(), "low")[2]["identity_status"], expected)


@override_settings(FACE_FIDELITY_ENABLED=True)
class FaceFidelityTests(SimpleTestCase):
    def check(self, reference, candidate, boxes=WHOLE_IMAGE):
        with patch.object(face_fidelity, "_detect_boxes", return_value=boxes):
            return check_face_regions(reference, candidate)

    def test_a_faithful_face_passes_untouched(self):
        face = skin()
        candidate = cv2.add(face, 2)
        output, metadata = self.check(face, candidate)

        self.assertIs(output, candidate)
        self.assertEqual((metadata["status"], metadata["faces_checked"], metadata["faces_adjusted"]), ("passed", 1, 0))
        self.assertEqual(metadata["faces"][0]["violations_at_full_strength"], [])

    def test_waxy_skin_is_detected_and_pulled_back_toward_the_reference(self):
        face = skin()
        waxy = cv2.bilateralFilter(cv2.GaussianBlur(face, (0, 0), 3.0), 9, 60, 9)
        output, metadata = self.check(face, waxy)

        record = metadata["faces"][0]
        self.assertIn("texture_loss", record["violations_at_full_strength"])
        self.assertLess(record["applied_strength"], 1.0)
        self.assertEqual(metadata["faces_adjusted"], 1)
        before = face_fidelity.face_metrics(face, waxy)["texture_ratio"]
        after = face_fidelity.face_metrics(face, output)["texture_ratio"]
        self.assertLess(before, 0.6)
        self.assertGreater(after, before)

    def test_oversharpening_and_halos_are_detected(self):
        face = skin()
        blurred = cv2.GaussianBlur(face, (0, 0), 2.0)
        oversharpened = np.clip(face.astype(np.float32) + (face.astype(np.float32) - blurred) * 3.0, 0, 255).astype(np.uint8)
        _output, metadata = self.check(face, oversharpened)

        self.assertIn("texture_gain", metadata["faces"][0]["violations_at_full_strength"])
        self.assertLess(metadata["faces"][0]["applied_strength"], 1.0)

    def test_a_skin_tone_shift_is_detected(self):
        face = skin()
        shifted = face.copy()
        shifted[:, :, 1] = np.clip(shifted[:, :, 1].astype(np.int16) + 30, 0, 255).astype(np.uint8)
        output, metadata = self.check(face, shifted)

        self.assertIn("chroma_shift", metadata["faces"][0]["violations_at_full_strength"])
        self.assertLess(
            face_fidelity.face_metrics(face, output)["mean_chroma_shift"],
            face_fidelity.face_metrics(face, shifted)["mean_chroma_shift"],
        )

    def test_blown_highlights_on_a_face_are_detected(self):
        face = skin()
        blown = np.clip(face.astype(np.int16) + 110, 0, 255).astype(np.uint8)
        _output, metadata = self.check(face, blown)
        self.assertIn("highlight_clipping", metadata["faces"][0]["violations_at_full_strength"])

    def test_only_the_face_region_is_changed(self):
        scene = np.full((240, 320, 3), 90, dtype=np.uint8)
        scene[40:200, 80:240] = skin()
        candidate = scene.copy()
        candidate[40:200, 80:240] = cv2.GaussianBlur(scene[40:200, 80:240], (0, 0), 4.0)
        candidate[:30] = 200  # a change far from the face
        output, metadata = self.check(scene, candidate, boxes=[(100 / 320, 60 / 240, 120 / 320, 120 / 240)])

        self.assertEqual(metadata["faces_adjusted"], 1)
        np.testing.assert_array_equal(output[:30], candidate[:30])
        self.assertFalse(np.array_equal(output[60:180, 100:220], candidate[60:180, 100:220]))

    def test_a_faithful_upscale_is_judged_on_the_reference_grid_and_passes(self):
        face = skin()
        upscaled = cv2.resize(face, (320, 320), interpolation=cv2.INTER_LANCZOS4)
        output, metadata = self.check(face, upscaled)

        self.assertEqual(metadata["status"], "passed")
        self.assertEqual(output.shape, (320, 320, 3))

    def test_an_upscaler_that_smooths_a_face_is_caught_on_the_larger_grid(self):
        face = skin()
        smoothed = cv2.GaussianBlur(cv2.resize(face, (320, 320), interpolation=cv2.INTER_CUBIC), (0, 0), 5.0)
        output, metadata = self.check(face, smoothed)

        self.assertIn("texture_loss", metadata["faces"][0]["violations_at_full_strength"])
        self.assertEqual(output.shape, (320, 320, 3))

    def test_no_face_means_nothing_is_changed(self):
        candidate = cv2.GaussianBlur(skin(), (0, 0), 4.0)
        output, metadata = self.check(skin(), candidate, boxes=[])
        self.assertIs(output, candidate)
        self.assertEqual(metadata["status"], "no_faces")

    def test_a_missing_detector_skips_the_check_without_failing(self):
        candidate = skin()
        with patch.object(face_fidelity, "_detect_boxes", side_effect=ValidationError("no detector")):
            with self.assertLogs("enhancement.face_fidelity", level="WARNING"):
                output, metadata = check_face_regions(skin(), candidate)
        self.assertIs(output, candidate)
        self.assertEqual(metadata["status"], "unavailable")

    def test_no_face_coordinates_are_recorded(self):
        _output, metadata = self.check(skin(), cv2.add(skin(), 2))
        self.assertNotIn("box", str(metadata))

    @override_settings(**NO_MODELS)
    def test_the_pipeline_skips_the_check_when_nothing_changed_after_the_reference(self):
        flat = np.full((720, 960, 3), 130, dtype=np.uint8)
        with patch.object(face_fidelity, "_detect_boxes") as detect:
            parameters = enhance_bgr(flat, "low")[2]
        if parameters["face_fidelity"]["status"] == "not_required":
            detect.assert_not_called()
        self.assertEqual(parameters["face_fidelity"]["faces_adjusted"], 0)

    @override_settings(**NO_MODELS)
    def test_the_pipeline_reduces_a_late_stage_that_waxes_a_face(self):
        from . import services

        def waxing_stage(context):
            context.current_image = cv2.GaussianBlur(context.current_image, (0, 0), 5.0)
            return context

        tail = (("sharpen", waxing_stage, True, None), *services._TAIL_STAGES[1:])
        source = skin(200, 200)
        with patch.object(services, "_TAIL_STAGES", tail), \
                patch.object(face_fidelity, "_detect_boxes", return_value=[(0.1, 0.1, 0.8, 0.8)]):
            _output, _level, parameters = enhance_bgr(source, "low")

        self.assertEqual(parameters["face_fidelity"]["faces_adjusted"], 1)
        self.assertEqual(report(parameters, "face_fidelity")["outcome"], parameters["face_fidelity"]["status"])
        self.assertTrue(any("reduced on a face" in warning for warning in parameters["warnings"]))

    @override_settings(**NO_MODELS, FACE_FIDELITY_ENABLED=False)
    def test_it_can_be_switched_off(self):
        self.assertEqual(enhance_bgr(textured(), "low")[2]["face_fidelity"]["status"], "disabled")


class CheckpointTests(SimpleTestCase):
    @override_settings(**NO_MODELS)
    def test_the_checkpoint_runs_before_each_expensive_stage(self):
        seen = []
        enhance_bgr(textured(), "low", checkpoint=seen.append)
        self.assertEqual(seen[:2], ["exposure_enhancement", "adaptive_denoise"])
        self.assertIn("identity_guard", seen)
        self.assertNotIn("output_guard_a", seen)

    @override_settings(**NO_MODELS)
    def test_cancelling_stops_before_the_next_expensive_stage(self):
        reached = []

        def cancel_at_denoise(stage):
            reached.append(stage)
            if stage == "adaptive_denoise":
                raise EnhancementCancelled()

        with self.assertRaises(EnhancementCancelled):
            enhance_bgr(textured(), "low", checkpoint=cancel_at_denoise)
        self.assertEqual(reached, ["exposure_enhancement", "adaptive_denoise"])


class EncodedOutputTests(SimpleTestCase):
    def test_a_jpeg_is_decoded_and_measured_before_it_is_saved(self):
        image = textured(120, 160)
        payload = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
        record = _verify_encoded_output(payload, image, "jpeg")

        self.assertTrue(record["verified"])
        self.assertEqual((record["dimensions"], record["channels"]), ([160, 120], 3))
        self.assertLess(record["mean_absolute_error"], 2.0)
        self.assertGreater(record["psnr_db"], 35)

    def test_a_png_must_match_exactly(self):
        image = textured(120, 160)
        payload = cv2.imencode(".png", image)[1].tobytes()
        self.assertEqual(_verify_encoded_output(payload, image, "png")["mean_absolute_error"], 0.0)
        with self.assertRaisesRegex(ValidationError, "lost more detail"):
            _verify_encoded_output(payload, cv2.add(image, 1), "png")

    def test_a_truncated_file_is_refused(self):
        image = textured(120, 160)
        payload = cv2.imencode(".png", image)[1].tobytes()
        with self.assertRaisesRegex(ValidationError, "could not be read back"):
            _verify_encoded_output(payload[:40], image, "png")

    def test_the_wrong_size_is_refused(self):
        image = textured(120, 160)
        payload = cv2.imencode(".jpg", image[:60])[1].tobytes()
        with self.assertRaisesRegex(ValidationError, "does not match"):
            _verify_encoded_output(payload, image, "jpeg")

    @override_settings(ENHANCEMENT_MAX_ENCODING_ERROR=0.5)
    def test_a_lossy_file_beyond_the_limit_is_refused(self):
        image = np.random.default_rng(1).integers(0, 256, (120, 160, 3), dtype=np.uint8)
        payload = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 30])[1].tobytes()
        with self.assertRaisesRegex(ValidationError, "lost more detail"):
            _verify_encoded_output(payload, image, "jpeg")


@override_settings(**NO_MODELS)
class ClaimTests(ResultFixture):
    def test_a_completed_result_clears_its_claim_and_records_verification(self):
        result = process_enhancement_result(self.make_result())

        self.assertEqual((result.status, result.claim_token), ("completed", ""))
        self.assertIsNotNone(result.claimed_at)
        self.assertTrue(result.applied_parameters["encoded_output"]["verified"])
        self.assertEqual(result.applied_parameters["encoded_output"]["bytes"], result.output_file_size)

    def test_a_result_another_worker_is_enhancing_is_refused_not_duplicated(self):
        result = self.make_result(status="processing")
        ImageEnhancementResult.objects.filter(pk=result.pk).update(claim_token="a" * 32, claimed_at=timezone.now())

        with patch("enhancement.services.enhance_bgr") as enhance:
            with self.assertRaises(ResultInProgress) as raised:
                process_enhancement_result(result)

        enhance.assert_not_called()
        self.assertGreater(raised.exception.retry_after, 0)
        result.refresh_from_db()
        self.assertEqual((result.status, result.claim_token), ("processing", "a" * 32))

    @override_settings(ENHANCEMENT_CLAIM_LEASE_SECONDS=600)
    def test_the_claim_of_a_worker_that_died_is_taken_over(self):
        result = self.make_result(status="processing")
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            claim_token="a" * 32, claimed_at=timezone.now() - timedelta(seconds=601),
        )
        self.assertEqual(process_enhancement_result(result).status, "completed")

    def test_each_stage_refreshes_the_claim(self):
        result = self.make_result()
        stale = timezone.now() - timedelta(minutes=10)
        seen = []
        real = enhance_bgr

        def spying(*args, **kwargs):
            ImageEnhancementResult.objects.filter(pk=result.pk).update(claimed_at=stale)
            kwargs["checkpoint"]("adaptive_denoise")
            seen.append(ImageEnhancementResult.objects.get(pk=result.pk).claimed_at)
            return real(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=spying):
            process_enhancement_result(result)
        self.assertGreater(seen[0], stale)

    def test_a_worker_that_lost_its_claim_publishes_nothing(self):
        result = self.make_result()
        real = enhance_bgr

        def taken_over_meanwhile(*args, **kwargs):
            output = real(*args, **{**kwargs, "checkpoint": None})
            ImageEnhancementResult.objects.filter(pk=result.pk).update(claim_token="b" * 32)
            return output

        with patch("enhancement.services.enhance_bgr", side_effect=taken_over_meanwhile):
            with self.assertLogs("enhancement.services", level="WARNING"):
                with self.assertRaises(EnhancementSuperseded):
                    process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual((result.status, result.claim_token), ("processing", "b" * 32))  # the new owner's row, untouched
        self.assertFalse(result.enhanced_file)

    def test_a_result_the_reaper_already_failed_is_not_overwritten_by_a_late_finish(self):
        result = self.make_result()
        real = enhance_bgr

        def reaped_meanwhile(*args, **kwargs):
            output = real(*args, **{**kwargs, "checkpoint": None})
            ImageEnhancementResult.objects.filter(pk=result.pk).update(status="failed", failure_reason="reaped")
            return output

        with patch("enhancement.services.enhance_bgr", side_effect=reaped_meanwhile):
            with self.assertLogs("enhancement.services", level="WARNING"):
                with self.assertRaises(EnhancementSuperseded):
                    process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual((result.status, result.failure_reason), ("failed", "reaped"))
        self.assertFalse(result.enhanced_file)

    def test_a_failure_by_a_worker_that_lost_its_claim_does_not_fail_the_new_owners_row(self):
        result = self.make_result()

        def lose_claim_then_crash(*args, **kwargs):
            ImageEnhancementResult.objects.filter(pk=result.pk).update(claim_token="b" * 32)
            raise RuntimeError("CUDA out of memory")

        with patch("enhancement.services.enhance_bgr", side_effect=lose_claim_then_crash):
            with self.assertLogs("enhancement.services", level="ERROR"):
                with self.assertRaises(ValidationError):
                    process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual(result.status, "processing")


@override_settings(**NO_MODELS)
class CancellationTests(ResultFixture):
    def cancel(self):
        type(self.batch).objects.filter(pk=self.batch.pk).update(cancellation_requested=True)

    def test_cancelling_mid_pipeline_stops_it_and_saves_nothing(self):
        result = self.make_result()
        real = enhance_bgr

        def cancel_during(*args, **kwargs):
            self.cancel()
            return real(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=cancel_during):
            with self.assertRaises(EnhancementCancelled):
                process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.claim_token, "")
        self.assertFalse(result.enhanced_file)
        self.assertIn("cancelled", result.failure_reason)

    def test_cancelling_after_the_pipeline_but_before_publishing_saves_nothing(self):
        result = self.make_result()
        real = enhance_bgr

        def cancel_after(*args, **kwargs):
            output = real(*args, **kwargs)
            self.cancel()
            return output

        with patch("enhancement.services.enhance_bgr", side_effect=cancel_after):
            with self.assertRaises(EnhancementCancelled):
                process_enhancement_result(result)

        result.refresh_from_db()
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.enhanced_file)

    def test_the_single_image_task_reports_the_cancellation_without_failing_it(self):
        result = self.make_result()
        real = enhance_bgr

        def cancel_during(*args, **kwargs):
            self.cancel()
            return real(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=cancel_during):
            outcome = process_enhancement_image.apply(args=[result.pk]).get()
        self.assertEqual(outcome["status"], "cancelled")

    def test_a_batch_cancelled_during_its_last_image_is_closed_as_cancelled(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=2,
        )
        results = [self.make_result(f"{index}.jpg", job=job) for index in range(2)]
        real = enhance_bgr
        calls = []

        def cancel_on_last(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.cancel()
            return real(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=cancel_on_last):
            outcome = process_enhancement_batch.run(job.pk)

        job.refresh_from_db()
        self.assertEqual((outcome["status"], job.status), ("cancelled", "cancelled"))
        statuses = [ImageEnhancementResult.objects.get(pk=row.pk).status for row in results]
        self.assertEqual(statuses, ["completed", "cancelled"])


@override_settings(**NO_MODELS)
class DuplicateDeliveryTaskTests(ResultFixture):
    def claim_elsewhere(self, result):
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            status="processing", claim_token="a" * 32, claimed_at=timezone.now(),
        )

    def test_a_duplicate_single_image_task_leaves_the_running_one_alone(self):
        result = self.make_result()
        self.claim_elsewhere(result)

        outcome = process_enhancement_image.apply(args=[result.pk]).get()

        self.assertEqual(outcome["status"], "in_progress")
        result.refresh_from_db()
        self.assertEqual((result.status, result.claim_token, result.failure_reason), ("processing", "a" * 32, ""))

    def test_a_duplicate_batch_skips_the_image_another_worker_owns(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=2,
        )
        owned, free = (self.make_result(f"{index}.jpg", job=job) for index in range(2))
        self.claim_elsewhere(owned)

        with self.assertLogs("enhancement.tasks", level="WARNING"):
            process_enhancement_batch.run(job.pk)

        owned.refresh_from_db()
        free.refresh_from_db()
        self.assertEqual((owned.status, owned.claim_token), ("processing", "a" * 32))  # not failed, not redone
        self.assertEqual(free.status, "completed")

    def test_running_the_same_result_twice_in_a_row_enhances_it_once(self):
        result = self.make_result()
        with patch("enhancement.services.enhance_bgr", wraps=enhance_bgr) as enhance:
            process_enhancement_image.apply(args=[result.pk])
            process_enhancement_image.apply(args=[result.pk])
        self.assertEqual(enhance.call_count, 1)


@override_settings(**NO_MODELS)
class TransparencyTests(ResultFixture):
    def make_transparent_result(self, output_format="png"):
        rgb = cv2.cvtColor(textured(64, 96, base=90), cv2.COLOR_BGR2RGB)
        alpha = np.zeros((64, 96), dtype=np.uint8)
        alpha[8:56, 12:84] = 255
        alpha[8:56, 12:20] = 128  # a soft edge
        buffer = io.BytesIO()
        Image.fromarray(np.dstack((rgb, alpha)), "RGBA").save(buffer, format="PNG")
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="cutout.png", file_extension=".png",
            mime_type="image/png", file_size=buffer.tell(), width=96, height=64, status="uploaded",
        )
        image.original_file.save("cutout.png", ContentFile(buffer.getvalue()))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format=output_format,
            output_name=f"cutout.{'png' if output_format == 'png' else 'jpg'}", output_width=96, output_height=64,
            had_transparency=True,
        )
        preprocessing.processed_file.save("cutout.png", ContentFile(buffer.getvalue()))
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=preprocessing,
            status="queued", requested_level=EnhancementLevel.MEDIUM,
        )
        return result, alpha

    def read_output(self, result):
        with result.enhanced_file.open("rb") as handle:
            return cv2.imdecode(np.frombuffer(handle.read(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)

    def test_a_png_keeps_its_transparency_exactly(self):
        result, alpha = self.make_transparent_result()
        result = process_enhancement_result(result)

        output = self.read_output(result)
        self.assertEqual(output.shape, (64, 96, 4))
        np.testing.assert_array_equal(output[:, :, 3], alpha)
        self.assertEqual(result.applied_parameters["transparency"], {"source_had_alpha": True, "preserved": True})
        self.assertEqual(result.applied_parameters["encoded_output"]["channels"], 4)
        self.assertEqual(result.output_format, "png")

    def test_the_visible_pixels_are_still_enhanced(self):
        result, _alpha = self.make_transparent_result()
        result = process_enhancement_result(result)

        output = self.read_output(result)
        source = textured(64, 96, base=90)
        self.assertGreater(float(output[20:50, 30:80, :3].mean()), float(source[20:50, 30:80].mean()))

    def test_a_jpeg_output_is_flattened_as_before(self):
        result, _alpha = self.make_transparent_result(output_format="jpeg")
        result = process_enhancement_result(result)

        self.assertEqual(self.read_output(result).shape, (64, 96, 3))
        self.assertEqual(result.applied_parameters["transparency"], {"source_had_alpha": False, "preserved": False})


def noisy(image, sigma, seed=9):
    rng = np.random.default_rng(seed)
    return np.clip(image.astype(np.float32) + rng.normal(0, sigma, image.shape), 0, 255).astype(np.uint8)


@override_settings(**NO_MODELS)
class EvaluationDrivenBehaviourTests(SimpleTestCase):
    """Behaviour changed because scripts/evaluate_pipeline_cases.py showed a failure."""

    def similarity(self, first, second):
        from .services import _structural_similarity

        return _structural_similarity(first, second)

    def test_a_bright_grainy_photo_is_denoised_on_the_automatic_level(self):
        clean = textured(720, 960, base=135)
        grainy = noisy(clean, 16)
        output, level, parameters = enhance_bgr(grainy, "auto")

        self.assertEqual(level, "low")
        self.assertTrue(parameters["adaptive_denoise"]["applied"])
        self.assertEqual(parameters["adaptive_denoise"]["reason"], "measured_noise_accepted")
        self.assertGreater(self.similarity(output, clean), self.similarity(grainy, clean) + 0.05)
        self.assertLess(parameters["quality_after"]["estimated_noise"], parameters["quality_before"]["estimated_noise"])

    def test_the_exposure_level_does_not_decide_whether_grain_is_removed(self):
        """An explicit LOW used to mean "never denoise", whatever was measured."""
        grainy = noisy(textured(720, 960, base=135), 16)
        for level in ("low", "medium", "high", "auto"):
            parameters = enhance_bgr(grainy, level)[2]
            self.assertTrue(parameters["adaptive_denoise"]["applied"], level)
            self.assertFalse(parameters["adaptive_denoise"]["eligibility"]["depends_on_exposure_level"])

    def test_a_clean_photo_is_not_denoised_on_the_automatic_level(self):
        parameters = enhance_bgr(textured(720, 960, base=135), "auto")[2]
        self.assertFalse(parameters["adaptive_denoise"]["applied"])

    def test_brightening_a_noisy_dark_photo_does_not_leave_it_grainier(self):
        clean = textured(720, 960, base=70)
        grainy = noisy(clean, 14)
        output, _level, parameters = enhance_bgr(grainy, "auto")

        self.assertTrue(parameters["adaptive_denoise"]["applied"])
        self.assertGreaterEqual(parameters["adaptive_denoise"]["strength"], 10)
        self.assertGreater(parameters["adaptive_denoise"]["benefit"]["noise_removed"], 1.0)
        self.assertLess(self.similarity(grainy, clean), self.similarity(output, clean))

    def test_stronger_noise_gets_a_stronger_denoise(self):
        from .services import _adaptive_denoise_strength

        strengths = [_adaptive_denoise_strength(score) for score in (0.5, 1.5, 3.0, 4.0, 6.0, 8.0, 12.0)]
        self.assertEqual(strengths, [0, 3, 4, 7, 10, 13, 16])
        self.assertEqual(strengths, sorted(strengths))

    def test_an_overexposed_source_is_corrected_instead_of_vetoed_by_its_own_clipping(self):
        clean = textured(720, 960, base=120)
        blown = np.clip(clean.astype(np.float32) * 1.6 + 40, 0, 255).astype(np.uint8)
        output, level, parameters = enhance_bgr(blown, "auto")

        guard = parameters["quality_guards"]["guard_a"]
        self.assertGreater(parameters["quality_before"]["highlight_clipping_percent"], 20)
        self.assertEqual(level, "high")  # judged on clipping, not on darkness alone
        self.assertNotIn("absolute_highlight_clipping_limited", guard["reasons"])
        self.assertEqual(guard["applied_candidate_strength"], 1.0)
        self.assertLess(float(output.mean()), float(blown.mean()))

    def test_enhancement_still_may_not_create_blown_highlights(self):
        from .services import _quality_guard, analyze_image_quality

        source = textured(720, 960, base=150)
        blown = np.clip(source.astype(np.float32) * 1.5 + 30, 0, 255).astype(np.uint8)
        _guarded, quality, metadata = _quality_guard(
            source, blown, EnhancementLevel.HIGH, analyze_image_quality(source),
        )
        self.assertIn("highlight_clipping_limited", metadata["reasons"])
        self.assertLessEqual(quality["highlight_clipping_percent"], 5.0)

    def test_a_bright_photo_without_clipping_is_not_treated_as_overexposed(self):
        high_key = np.clip(textured(720, 960, base=185), 0, 240)  # bright by intent, nothing clipped
        _output, level, parameters = enhance_bgr(high_key, "auto")
        self.assertEqual(level, "low")
        self.assertLess(parameters["quality_before"]["highlight_clipping_percent"], 8)

    @override_settings(IDENTITY_GUARD_ENABLED=True, IDENTITY_PROVIDER="disabled")
    def test_identity_is_unavailable_not_inconclusive_when_no_model_is_configured(self):
        parameters = enhance_bgr(textured(), "low")[2]
        self.assertEqual(parameters["identity_status"], "unavailable")
        self.assertIn("No identity model is configured", parameters["identity_guard"]["fallback_reason"])



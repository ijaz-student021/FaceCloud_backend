"""The restoration chain: a blurred face is found and deblurred whatever the frame reads; a result says what was applied or why not."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
from django.test import SimpleTestCase, TestCase, override_settings

from . import restoration
from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import EngineConfig
from .services import enhance_bgr

FACE = (150, 100, 180, 220)


def analyzer_for(box=FACE):
    return ImageQualityAnalyzer(detector=base.StubDetector([(box, base.landmarks_for(box))]))


def blurred_face(image, sigma, box=FACE):
    x, y, w, h = box
    out = image.copy()
    out[y:y + h, x:x + w] = cv2.GaussianBlur(out[y:y + h, x:x + w], (0, 0), sigma)
    return out


class FaceBlurAnalysisTests(SimpleTestCase):
    def test_a_sharp_face_has_no_face_blur(self):
        report = analyzer_for().analyze(base.portrait())
        self.assertEqual(report.face_blur["status"], "none")
        self.assertNotIn("face_blur", report.conditions)

    def test_a_blurred_face_is_found_even_when_the_frame_reads_as_sharp(self):
        report = analyzer_for().analyze(blurred_face(base.portrait(), 6))
        self.assertEqual(report.face_blur["status"], "high")
        self.assertIn("face_blur", report.conditions)
        self.assertIn("motion_deblurring", report.recommended_operations)
        self.assertEqual(report.blur["status"], "none", "the frame still reads sharp: this is the case the frame measure misses")

    def test_a_sharp_face_in_front_of_a_blurred_background_is_not_blur(self):
        image = base.portrait()
        background = cv2.GaussianBlur(image, (0, 0), 8)
        x, y, w, h = FACE
        background[y:y + h, x:x + w] = image[y:y + h, x:x + w]
        report = analyzer_for().analyze(background)
        self.assertEqual(report.face_blur["status"], "none")
        self.assertNotIn("face_blur", report.conditions)
        self.assertNotIn("motion_deblurring", report.recommended_operations)

    def test_no_face_means_unknown_face_blur(self):
        report = ImageQualityAnalyzer(detector=base.StubDetector([])).analyze(base.portrait())
        self.assertEqual(report.face_blur["status"], "unknown")


@override_settings(**base.ALL_ON)
class RouterFaceBlurTests(SimpleTestCase):
    def step_for(self, report, done=()):
        return AdaptiveEnhancementRouter(EngineConfig()).next_step(report, set(done))

    def report(self, face_blur, **blur):
        report = analyzer_for().analyze(base.portrait())
        report.face_blur = {"status": face_blur, "detail": 12.0, "judged_on": "main face"}
        report.blur = {**report.blur, **blur}
        return report

    def test_a_strongly_blurred_face_is_deblurred_whatever_the_frame_reads(self):
        for frame in ({"status": "none", "type": "none"}, {"status": "high", "type": "defocus"}, {"status": "low", "type": "defocus"}):
            decision = self.step_for(self.report("high", **frame))
            deblur = [item for item in [decision.step] if item and item.operation == ops.DEBLUR]
            self.assertTrue(deblur, frame)
            self.assertIn("main face is blurred", deblur[0].reason)

    def test_a_soft_face_alone_does_not_run_the_motion_model(self):
        decision = self.step_for(self.report("low", status="none", type="none"), done={ops.LOW_LIGHT, ops.DENOISE})
        self.assertTrue(decision.step is None or decision.step.operation != ops.DEBLUR)

    def test_frame_defocus_with_a_sharp_face_is_still_left_alone(self):
        decision = self.step_for(self.report("none", status="high", type="defocus"), done={ops.LOW_LIGHT, ops.DENOISE})
        self.assertTrue(decision.step is None or decision.step.operation != ops.DEBLUR)
        self.assertTrue(any(item["operation"] == ops.DEBLUR and "defocus" in item["reason"] for item in decision.skipped))

    def test_the_reason_is_recorded_when_the_model_is_off_or_the_photo_is_compressed(self):
        with override_settings(NAFNET_DEBLUR_ENABLED=False):
            decision = self.step_for(self.report("high", status="none", type="none"), done={ops.LOW_LIGHT, ops.DENOISE})
            self.assertIn({"operation": ops.DEBLUR, "reason": "No deblurring model is switched on."}, decision.skipped)
        report = self.report("high", status="none", type="none")
        report.compression = {"status": "heavy", "blockiness": [2.0]}
        decision = self.step_for(report, done={ops.LOW_LIGHT, ops.DENOISE})
        self.assertTrue(any("compression" in item["reason"] for item in decision.skipped))


class ValidatorFaceAwareTests(SimpleTestCase):
    def test_an_upscaled_candidate_does_not_lose_its_face_to_the_enlargement(self):
        """The upscaled photograph's faces sit at twice the coordinates; that is not a lost face."""
        from .adaptive.analyzer import FaceObservation
        from .adaptive.validator import landmark_shift

        def face(scale):
            box = tuple(int(v * scale) for v in (150, 100, 180, 220))
            landmarks = [(x * scale, y * scale) for x, y in base.landmarks_for((150, 100, 180, 220))]
            return FaceObservation(box=box, confidence=0.9, landmarks=landmarks, area_ratio=0.1, short_edge=box[2],
                                   luminance={}, detail=80.0, visibility="good", quality="good")

        self.assertIsNone(landmark_shift([face(1)], [face(2)]), "without the scale the enlargement looks like a lost face")
        self.assertAlmostEqual(landmark_shift([face(1)], [face(2)], scale=2.0), 0.0, places=6)
        self.assertIsNone(landmark_shift([face(1)], [], scale=2.0), "a face that is really gone is still lost")
        moved = face(2)
        moved = FaceObservation(**{**moved.__dict__, "landmarks": [(x + 20, y) for x, y in moved.landmarks]})
        self.assertGreater(landmark_shift([face(1)], [moved], scale=2.0), 0.05)


    def test_the_upscale_allows_a_little_more_landmark_jitter_than_other_operations(self):
        from types import SimpleNamespace

        from .adaptive.analyzer import FaceObservation
        from .adaptive.validator import FinalImageQualityValidator, Limits

        def face(scale, dx=0.0):
            box = tuple(int(v * scale) for v in (150, 100, 180, 220))
            landmarks = [(x * scale + dx, y * scale) for x, y in base.landmarks_for((150, 100, 180, 220))]
            return FaceObservation(box=box, confidence=0.9, landmarks=landmarks, area_ratio=0.1, short_edge=box[2],
                                   luminance={}, detail=80.0, visibility="good", quality="good")

        limits = Limits()
        self.assertGreater(limits.face_landmark_shift_max_upscale, limits.face_landmark_shift_max)
        original = SimpleNamespace(faces=[face(1)], width=640)
        candidate = SimpleNamespace(faces=[face(2, dx=0.09 * 2 * 180)], width=1280)   # 9% of the (scaled) face width
        validator = FinalImageQualityValidator(None, original, identity=None)
        for operation, passes in (("super_resolution", True), ("motion_deblurring", False), ("face_restoration", False)):
            verdict = {"reasons": [], "gates": {}}
            validator._identity_gate(verdict, None, candidate, operation)
            self.assertEqual(verdict["gates"]["identity"]["passed"], passes, operation)


class ThresholdSettingsTests(SimpleTestCase):
    def test_the_new_thresholds_are_settings_with_documented_defaults(self):
        from .adaptive.analyzer import Thresholds
        from .adaptive.validator import Limits

        self.assertEqual((Thresholds().face_blur_high, Limits().face_deblur_min_detail_gain, Limits().face_landmark_shift_max_upscale),
                         (25.0, 1.25, 0.10))
        with override_settings(ADAPTIVE_FACE_BLUR_HIGH=40.0, ADAPTIVE_FACE_DEBLUR_MIN_DETAIL_GAIN=2.0, ADAPTIVE_UPSCALE_LANDMARK_SHIFT_MAX=0.1):
            self.assertEqual((Thresholds().face_blur_high, Limits().face_deblur_min_detail_gain, Limits().face_landmark_shift_max_upscale),
                             (40.0, 2.0, 0.1))
        with override_settings(ADAPTIVE_FACE_BLUR_HIGH=40.0):
            report = ImageQualityAnalyzer(detector=base.StubDetector([(FACE, base.landmarks_for(FACE))])).analyze(blurred_face(base.portrait(), 2.5))
            self.assertIn(report.face_blur["status"], ("high", "low"))


class StageStatusTests(SimpleTestCase):
    def test_a_stage_needs_its_switch_its_checkpoint_and_pytorch(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "nafnet.pth"
            checkpoint.write_bytes(b"x")
            with override_settings(NAFNET_DEBLUR_ENABLED=True, NAFNET_MODEL_PATH=str(checkpoint)), \
                    patch.object(restoration, "torch_importable", return_value=True):
                self.assertTrue(restoration.stage_status()["deblur"]["available"])
                self.assertEqual(restoration.stage_status()["deblur"]["engine"], "nafnet_gopro")
            with override_settings(NAFNET_DEBLUR_ENABLED=False, NAFNET_MODEL_PATH=str(checkpoint)), \
                    patch.object(restoration, "torch_importable", return_value=True):
                status = restoration.stage_status()["deblur"]
                self.assertFalse(status["available"])
                self.assertIn("switched off", status["reason"])
            with override_settings(NAFNET_DEBLUR_ENABLED=True, NAFNET_MODEL_PATH=str(checkpoint)), \
                    patch.object(restoration, "torch_importable", return_value=False):
                self.assertIn("PyTorch is not installed", restoration.stage_status()["deblur"]["reason"])
            with override_settings(NAFNET_DEBLUR_ENABLED=True, NAFNET_MODEL_PATH=str(Path(folder) / "missing.pth")), \
                    patch.object(restoration, "torch_importable", return_value=True):
                self.assertIn("checkpoint not found", restoration.stage_status()["deblur"]["reason"])

    def test_the_defaults_switch_on_only_the_permissively_licensed_stages(self):
        from django.conf import settings

        self.assertTrue(settings.HD_RESTORATION_ENABLED)
        self.assertTrue(settings.NAFNET_DEBLUR_ENABLED)
        self.assertFalse(settings.CODEFORMER_ENABLED)
        self.assertFalse(settings.GFPGAN_ENABLED)


class DescribeStagesTests(SimpleTestCase):
    def adaptive(self, stages, recommended, skipped=()):
        return {
            "resolution_contract": {"source_dimensions": [480, 360], "actual_output_dimensions": [960, 720]},
            "adaptive": {"stages": stages, "operations_skipped": list(skipped), "quality_report": {"recommended_operations": recommended}},
        }

    def test_applied_stages_name_the_engine_and_the_upscale_sizes(self):
        parameters = self.adaptive([
            {"operation": "motion_deblurring", "status": "accepted", "models_run": ["nafnet_gopro"], "variant": "blend 1.0"},
            {"operation": "super_resolution", "status": "accepted", "models_run": ["realesrgan"], "variant": "x2"},
        ], ["motion_deblurring", "super_resolution"])
        described = restoration.describe_stages(parameters)
        self.assertEqual([s["stage"] for s in described["applied_stages"]], ["deblur", "upscale"])
        upscale = described["applied_stages"][1]
        self.assertEqual((upscale["engine"], upscale["scale"], upscale["input_size"], upscale["output_size"]),
                         ("realesrgan", 2, [480, 360], [960, 720]))
        self.assertIsNone(described["hd_unavailable_reason"])
        self.assertEqual(described["skipped_stages"], [])

    def test_a_needed_stage_that_could_not_run_says_why(self):
        parameters = self.adaptive([
            {"operation": "super_resolution", "status": "unavailable", "skipped_engines": [
                {"engine": "realesrgan", "reason": "The Real-ESRGAN checkpoint is unavailable."}], "notes": []},
        ], ["super_resolution"])
        described = restoration.describe_stages(parameters)
        self.assertEqual(described["applied_stages"], [])
        self.assertEqual(described["skipped_stages"][0]["stage"], "upscale")
        self.assertIn("Real-ESRGAN checkpoint is unavailable", described["hd_unavailable_reason"])

    def test_a_stage_skipped_by_the_router_is_reported_with_its_reason(self):
        parameters = self.adaptive([], ["motion_deblurring"], skipped=[{"operation": "motion_deblurring", "reason": "No deblurring model is switched on."}])
        described = restoration.describe_stages(parameters)
        self.assertIn("No deblurring model is switched on.", described["hd_unavailable_reason"])

    def test_a_photo_that_needed_nothing_has_no_reason(self):
        described = restoration.describe_stages(self.adaptive([], []))
        self.assertEqual(described, {"applied_stages": [], "skipped_stages": [], "hd_unavailable_reason": None})

    def test_a_rejected_candidate_is_not_reported_as_applied(self):
        parameters = self.adaptive([
            {"operation": "motion_deblurring", "status": "rejected", "rejection_reasons": ["halos"], "skipped_engines": [], "notes": []},
        ], ["motion_deblurring"])
        described = restoration.describe_stages(parameters)
        self.assertEqual(described["applied_stages"], [])
        self.assertIn("halos", described["hd_unavailable_reason"])

    def test_the_staged_pipeline_reports_what_the_up_front_check_found(self):
        parameters = {"restoration_check": {"needed": ["deblur", "upscale"], "unavailable": {"deblur": "nafnet_gopro: checkpoint not found"}}}
        described = restoration.describe_stages(parameters)
        self.assertEqual({s["stage"] for s in described["skipped_stages"]}, {"deblur", "upscale"})
        self.assertIn("checkpoint not found", described["hd_unavailable_reason"])

    def test_a_failed_or_empty_result_has_nothing_to_report(self):
        for parameters, failed in (({}, False), (None, False), (self.adaptive([], ["super_resolution"]), True)):
            self.assertEqual(restoration.describe_stages(parameters, failed=failed)["applied_stages"], [])
            self.assertIsNone(restoration.describe_stages(parameters, failed=failed)["hd_unavailable_reason"])


class EscalationTests(SimpleTestCase):
    image = base.portrait()

    def run_enhance(self, check, **overrides):
        sentinel = (self.image.copy(), "medium", {"adaptive": {"outcome": "enhanced"}})
        settings_override = dict(ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RESTORATION_ESCALATION=True, **overrides)
        with override_settings(**settings_override), \
                patch.object(restoration, "restoration_check", return_value=check) as checker, \
                patch("enhancement.adaptive.service.AdaptiveImageEnhancementService.enhance", return_value=sentinel) as adaptive:
            result = enhance_bgr(self.image)
        return result, checker, adaptive

    def test_the_chain_does_what_it_can_when_only_some_needed_stages_are_available(self):
        report = analyzer_for().analyze(blurred_face(base.portrait(), 6))
        with patch.object(restoration, "stage_status", return_value={
            "deblur": {"available": True, "reason": ""}, "upscale": {"available": True, "reason": ""},
            "face_restore": {"available": False, "reason": "codeformer: switched off"},
        }):
            check = restoration.restoration_check(base.portrait(), analyzer=type("A", (), {"analyze": lambda self, image: report})())
        self.assertIn("deblur", check["needed"])
        self.assertTrue(check["available"])
        self.assertEqual(set(check["unavailable"]), {"face_restore"} & set(check["needed"]))

    def test_a_photo_that_needs_restoration_and_can_get_it_goes_through_the_chain(self):
        check = {"needed": ["deblur"], "available": True, "unavailable": {}, "analysed": True}
        (image, level, parameters), checker, adaptive = self.run_enhance(check)
        adaptive.assert_called_once()
        self.assertEqual(parameters["restoration_escalation"]["from_policy"], "lightweight_v1")
        self.assertIn("deblur", parameters["restoration_escalation"]["reason"])

    def test_when_it_cannot_run_here_the_policys_pipeline_runs_and_the_reason_is_recorded(self):
        check = {"needed": ["upscale"], "available": False, "unavailable": {"upscale": "realesrgan: checkpoint not found"}, "analysed": True}
        (image, level, parameters), checker, adaptive = self.run_enhance(check)
        adaptive.assert_not_called()
        self.assertEqual(parameters["restoration_check"]["needed"], ["upscale"])
        described = restoration.describe_stages(parameters)
        self.assertIn("checkpoint not found", described["hd_unavailable_reason"])
        self.assertEqual(described["applied_stages"], [])

    def test_a_photo_that_needs_nothing_is_untouched_by_the_check(self):
        (image, level, parameters), checker, adaptive = self.run_enhance({"needed": [], "available": False, "unavailable": {}, "analysed": True})
        adaptive.assert_not_called()
        self.assertNotIn("restoration_check", parameters)
        self.assertNotIn("restoration_escalation", parameters)

    def test_the_escalation_can_be_switched_off(self):
        with override_settings(ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RESTORATION_ESCALATION=False), \
                patch.object(restoration, "restoration_check") as checker:
            enhance_bgr(self.image)
        checker.assert_not_called()

    def test_the_adaptive_policy_is_not_checked_twice(self):
        sentinel = (self.image.copy(), "medium", {"adaptive": {}})
        with override_settings(ENHANCEMENT_RENDER_POLICY="adaptive_v1"), patch.object(restoration, "restoration_check") as checker, \
                patch("enhancement.adaptive.service.AdaptiveImageEnhancementService.enhance", return_value=sentinel):
            enhance_bgr(self.image)
        checker.assert_not_called()

    def test_an_analysis_failure_never_fails_the_enhancement(self):
        class Broken:
            def analyze(self, image):
                raise RuntimeError("boom")

        check = restoration.restoration_check(self.image, analyzer=Broken())
        self.assertEqual((check["needed"], check["available"], check["analysed"]), ([], False, False))


class ResultApiTests(TestCase):
    """The new fields are on the enhancement result the API returns, with the existing ones unchanged."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

        self.user = get_user_model().objects.create_user(
            username="restore", email="restore@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        image = ImageUpload.objects.create(owner=self.user, batch=batch, original_name="a.jpg", status="uploaded")
        self.pre = ImagePreprocessingResult.objects.create(owner=self.user, image=image, status="completed")
        self.image = image
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def result(self, parameters, status="completed"):
        from .models import ImageEnhancementResult

        return ImageEnhancementResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.pre, requested_level="auto",
            applied_level="medium", status=status, applied_parameters=parameters,
        )

    def fetch(self, result):
        from django.urls import reverse

        response = self.client.get(reverse("enhancement:result-detail", args=[result.pk]))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_applied_stages_skipped_stages_and_the_reason_are_returned(self):
        done = self.result({
            "resolution_contract": {"source_dimensions": [480, 360], "actual_output_dimensions": [960, 720]},
            "adaptive": {"outcome": "enhanced", "summary": "x", "quality_report": {"recommended_operations": ["super_resolution"]},
                         "stages": [{"operation": "super_resolution", "status": "accepted", "models_run": ["realesrgan"], "variant": "x2"}],
                         "operations_skipped": []},
        })
        body = self.fetch(done)
        self.assertEqual(body["applied_stages"][0]["stage"], "upscale")
        self.assertEqual(body["applied_stages"][0]["output_size"], [960, 720])
        self.assertEqual((body["skipped_stages"], body["hd_unavailable_reason"]), ([], None))
        for key in ("enhancement_outcome", "enhancement_summary", "applied_parameters", "download_url"):
            self.assertIn(key, body)

    def test_a_needed_but_unavailable_restoration_is_never_silent(self):
        body = self.fetch(self.result({"restoration_check": {"needed": ["deblur"], "unavailable": {"deblur": "nafnet_gopro: checkpoint not found"}}}))
        self.assertEqual(body["applied_stages"], [])
        self.assertIn("checkpoint not found", body["hd_unavailable_reason"])

    def test_a_failed_result_reports_nothing(self):
        body = self.fetch(self.result({}, status="failed"))
        self.assertEqual((body["applied_stages"], body["hd_unavailable_reason"]), ([], None))

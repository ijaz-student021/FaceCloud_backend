"""The adaptive enhancement engine (enhancement/adaptive/): analyzer, router, validator, candidates and the service.

Models are replaced by stand-ins here, because these tests are about *decisions*: what is analysed, which operation is
chosen, what is accepted or rejected and why, and what outcome is reported. Real-model behaviour is exercised in
test_adaptive_real_models.py (skipped when the checkpoints or PyTorch are absent). A passing test here does not show that
a photograph looks better; the benchmark and the visual evaluation do that.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from validation.services import FaceDetection

from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer, blur_signals
from .adaptive.candidates import CandidatePool, PoolFull
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import (
    ENHANCED, ORIGINAL_PRESERVED, PARTIALLY_ENHANCED, AdaptiveImageEnhancementService, EngineConfig,
)
from .adaptive.validator import FinalImageQualityValidator, Limits, reference_metrics
from .test_blur_measure import dead_leaves, resized
from .test_staged_pipeline import skin

ALL_ON = dict(
    ENHANCEMENT_RENDER_POLICY="adaptive_v1", RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=False, NAFNET_DENOISE_ENABLED=True,
    NAFNET_DEBLUR_ENABLED=True, HD_RESTORATION_ENABLED=True, CODEFORMER_ENABLED=True, GFPGAN_ENABLED=False,
    ADAPTIVE_DEBUG_ARTIFACTS=False,
)


class StubDetector:
    """Returns the given faces for any image (a face detector is not what these tests are about)."""

    def __init__(self, faces=()):
        self.faces = list(faces)

    def detect(self, image):
        boxes = [tuple(box) for box, _ in self.faces]
        landmarks = [points for _, points in self.faces]
        return FaceDetection(boxes=boxes, scores=[0.99] * len(boxes), landmarks=landmarks, landmark_scores=[0.99] * len(boxes),
                             detector="stub", detector_version="1", confidence_type="stub")


def landmarks_for(box):
    x, y, w, h = box
    return [(x + 0.3 * w, y + 0.38 * h), (x + 0.7 * w, y + 0.38 * h), (x + 0.5 * w, y + 0.58 * h),
            (x + 0.35 * w, y + 0.78 * h), (x + 0.65 * w, y + 0.78 * h)]


def portrait(size=(640, 480), brightness=1.0, face_box=(150, 100, 180, 220), noise=0.0, seed=3):
    """A textured background with a skin-toned face patch; ``brightness`` scales it, ``noise`` adds Gaussian noise."""
    height, width = size
    background = cv2.resize(dead_leaves(900), (width, height), interpolation=cv2.INTER_AREA).astype(np.float32) * 0.55 + 40
    x, y, w, h = face_box
    face = cv2.resize(skin(200, 180), (w, h), interpolation=cv2.INTER_CUBIC).astype(np.float32)
    face = face.mean() + (face - face.mean()) * 2.5  # a real face has more tonal range than the synthetic patch
    background[y:y + h, x:x + w] = face
    image = np.clip(background * brightness, 0, 255)
    if noise:
        image = image + np.random.default_rng(seed).normal(0, noise, image.shape)
    return np.clip(image, 0, 255).astype(np.uint8)


def analyzer_with(face_box=(150, 100, 180, 220)):
    faces = [(face_box, landmarks_for(face_box))] if face_box else []
    return ImageQualityAnalyzer(detector=StubDetector(faces))


def motion_blur(image, length=21, angle=0):
    kernel = np.zeros((length, length), np.float32)
    cv2.line(kernel, (0, length // 2), (length - 1, length // 2), 1, 1)
    matrix = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    kernel = cv2.warpAffine(kernel, matrix, (length, length))
    return cv2.filter2D(image, -1, kernel / kernel.sum())


class AnalyzerTests(SimpleTestCase):
    def test_a_well_exposed_photograph_has_nothing_to_fix(self):
        report = analyzer_with().analyze(portrait())
        self.assertEqual(report.exposure["status"], "normal")
        self.assertEqual(report.recommended_operations, [])
        self.assertEqual(report.face_count, 1)

    def test_a_crushed_face_is_low_light_but_a_dark_scene_with_a_visible_face_is_not(self):
        dark = analyzer_with().analyze(portrait(brightness=0.12))
        self.assertEqual(dark.exposure["status"], "low_light_subject")
        self.assertIn("low_light_enhancement", dark.recommended_operations)
        # a dark scene whose face stays well lit (a flash, a lamp): the face is lit, so this is not a defect
        scene = portrait(brightness=0.35)
        x, y, w, h = (150, 100, 180, 220)
        scene[y:y + h, x:x + w] = np.clip(skin(h, w).astype(np.float32) * 1.05, 0, 255).astype(np.uint8)
        lit = analyzer_with().analyze(scene)
        self.assertNotEqual(lit.exposure["status"], "low_light_subject")
        self.assertNotIn("low_light_enhancement", lit.recommended_operations)

    def test_a_blurred_face_in_a_well_lit_photograph_is_not_mistaken_for_low_light(self):
        """Found by the benchmark: blur narrows a face's tonal range, which once read as under-exposure."""
        blurred = portrait(brightness=0.85)
        x, y, w, h = (150, 100, 180, 220)
        blurred[y:y + h, x:x + w] = cv2.GaussianBlur(blurred[y:y + h, x:x + w], (0, 0), 7.0)
        report = analyzer_with().analyze(blurred)
        self.assertNotIn(report.exposure["status"], ("low_light", "low_light_subject"))
        self.assertNotIn("low_light_enhancement", report.recommended_operations)

    def test_a_dark_face_in_a_bright_frame_is_backlit_not_low_light(self):
        scene = portrait(brightness=1.15)
        x, y, w, h = (150, 100, 180, 220)
        scene[y:y + h, x:x + w] = np.clip(scene[y:y + h, x:x + w].astype(np.float32) * 0.08, 0, 255).astype(np.uint8)
        report = analyzer_with().analyze(scene)
        self.assertEqual(report.exposure["status"], "backlit_subject")
        self.assertNotIn("low_light_enhancement", report.recommended_operations)

    def test_with_no_face_the_frame_decides(self):
        report = analyzer_with(None).analyze(portrait(brightness=0.1))
        self.assertEqual(report.exposure["status"], "low_light")
        self.assertEqual(report.exposure["judged_on"], "frame")
        self.assertEqual(report.face_count, 0)
        self.assertIn("no_face", report.conditions)

    def test_noise_is_banded_by_the_measured_estimate(self):
        statuses = [analyzer_with().analyze(portrait(noise=sigma)).noise["status"] for sigma in (0.0, 25.0)]
        self.assertEqual(statuses[0], "none")
        self.assertIn(statuses[1], ("moderate", "high"))

    def test_motion_blur_is_told_from_defocus(self):
        sharp = resized(dead_leaves(), 900)
        retention_sharp, _ = blur_signals(cv2.cvtColor(sharp, cv2.COLOR_BGR2GRAY))
        motion = analyzer_with(None).analyze(motion_blur(sharp, 25, 0))
        defocus = analyzer_with(None).analyze(cv2.GaussianBlur(sharp, (0, 0), 3.5))
        self.assertEqual(analyzer_with(None).analyze(sharp).blur["status"], "none")
        self.assertIn(motion.blur["status"], ("low", "high"))
        self.assertEqual(motion.blur["type"], "motion")
        self.assertEqual(defocus.blur["status"], "high")
        self.assertIn(defocus.blur["type"], ("defocus", "mixed"))
        self.assertNotEqual(defocus.blur["type"], "motion")
        self.assertLess(retention_sharp, 0.5)

    def test_a_small_face_and_a_small_photograph_are_reported(self):
        tiny = analyzer_with((40, 30, 36, 44)).analyze(portrait(face_box=(40, 30, 36, 44)))
        self.assertEqual(tiny.main_face.quality, "too_small")
        self.assertNotIn("face_restoration", tiny.recommended_operations)
        small = analyzer_with(None).analyze(cv2.resize(portrait(), (320, 240)))
        self.assertEqual(small.resolution["status"], "low")
        self.assertIn("super_resolution", small.recommended_operations)

    def test_the_report_is_structured_and_states_its_limits(self):
        data = analyzer_with().analyze(portrait()).as_dict()
        for key in ("brightness_status", "noise_status", "blur_status", "blur_type", "resolution_status", "face_count",
                    "face_quality", "recommended_operations", "limitations"):
            self.assertIn(key, data)
        self.assertTrue(data["limitations"])

    def test_a_detector_that_fails_does_not_fail_the_analysis(self):
        class Broken:
            def detect(self, image):
                raise RuntimeError("detector down")

        report = ImageQualityAnalyzer(detector=Broken()).analyze(portrait())
        self.assertEqual(report.face_count, 0)
        self.assertTrue(any("no face information" in item for item in report.limitations))

    def test_an_invalid_image_is_refused(self):
        with self.assertRaises(ValueError):
            ImageQualityAnalyzer(detector=StubDetector()).analyze(np.zeros((0, 0, 3), np.uint8))


@override_settings(**ALL_ON)
class RouterTests(SimpleTestCase):
    def router(self):
        return AdaptiveEnhancementRouter(EngineConfig())

    def test_a_clean_photograph_runs_nothing(self):
        decision = self.router().next_step(analyzer_with().analyze(portrait()), set())
        self.assertIsNone(decision.step)

    def test_a_dark_subject_gets_low_light_with_a_full_and_a_conservative_variant(self):
        decision = self.router().next_step(analyzer_with().analyze(portrait(brightness=0.12)), set())
        self.assertEqual(decision.step.operation, ops.LOW_LIGHT)
        self.assertEqual([v["blend"] for v in decision.step.variants], [1.0, 0.65])
        self.assertEqual(decision.step.engines, ["retinexformer", "opencv_exposure"])

    def test_the_order_is_fixed_and_done_operations_are_not_repeated(self):
        report = analyzer_with().analyze(portrait(brightness=0.12, noise=20))
        first = self.router().next_step(report, set()).step.operation
        second = self.router().next_step(report, {first}).step.operation
        self.assertEqual((first, second), (ops.LOW_LIGHT, ops.DENOISE))

    def test_defocus_blur_is_skipped_with_a_reason(self):
        report = analyzer_with(None).analyze(cv2.GaussianBlur(resized(dead_leaves(), 900), (0, 0), 3.5))
        decision = self.router().next_step(report, set())
        if report.blur["type"] == "defocus":
            self.assertIsNone(decision.step)
            self.assertTrue(any("defocus" in item["reason"].lower() for item in decision.skipped))

    def test_motion_blur_gets_the_deblurring_model(self):
        report = analyzer_with(None).analyze(motion_blur(resized(dead_leaves(), 900), 25, 0))
        decision = self.router().next_step(report, set())
        self.assertEqual(decision.step.operation, ops.DEBLUR)
        self.assertEqual(decision.step.engines, ["nafnet_gopro"])

    def test_a_tiny_face_is_never_restored_and_the_reason_is_recorded(self):
        report = analyzer_with((40, 30, 36, 44)).analyze(portrait(face_box=(40, 30, 36, 44)))
        decision = self.router().next_step(report, {ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR, ops.UPSCALE})
        self.assertIsNone(decision.step)
        self.assertTrue(any("too small" in item["reason"] for item in decision.skipped))

    def test_super_resolution_is_not_proposed_past_the_output_limit(self):
        report = analyzer_with(None).analyze(cv2.resize(portrait(), (320, 240)))
        tight = AdaptiveEnhancementRouter(EngineConfig(max_output_edge=400))
        decision = tight.next_step(report, {ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR, ops.FACE})
        self.assertIsNone(decision.step)
        self.assertTrue(any("size limit" in item["reason"] for item in decision.skipped))

    @override_settings(NAFNET_DEBLUR_ENABLED=False)
    def test_an_operation_with_its_model_off_is_reported_not_silently_dropped(self):
        report = analyzer_with(None).analyze(motion_blur(resized(dead_leaves(), 900), 25, 0))
        decision = self.router().next_step(report, set())
        self.assertIsNone(decision.step)
        self.assertTrue(any("switched on" in item["reason"] for item in decision.skipped))


class ValidatorTests(SimpleTestCase):
    def setUp(self):
        self.analyzer = analyzer_with()
        self.parent = portrait(brightness=0.12)
        self.report = self.analyzer.analyze(self.parent)
        self.validator = FinalImageQualityValidator(self.analyzer, self.report)

    def verdict(self, candidate, operation="low_light_enhancement", parent=None, report=None, **kwargs):
        return self.validator.evaluate(parent if parent is not None else self.parent, candidate, operation,
                                       report or self.report, **kwargs)

    def lifted(self, factor=6.0):
        return np.clip(self.parent.astype(np.float32) * factor, 0, 255).astype(np.uint8)

    def test_a_sensible_lift_is_accepted(self):
        verdict = self.verdict(self.lifted(5.0))
        self.assertTrue(verdict["accepted"], verdict["reasons"])
        self.assertGreater(verdict["improvement"], 0.15)

    def test_a_lift_that_over_brightens_is_rejected(self):
        verdict = self.verdict(np.clip(self.parent.astype(np.float32) * 14 + 60, 0, 255).astype(np.uint8))
        self.assertFalse(verdict["accepted"])

    def test_a_candidate_with_the_wrong_size_is_rejected_first(self):
        verdict = self.verdict(cv2.resize(self.lifted(), (100, 100)))
        self.assertEqual(verdict["reasons"], ["wrong_dimensions"])

    def test_colour_drift_is_rejected(self):
        candidate = self.lifted(5.0)
        candidate[:, :, 2] = np.clip(candidate[:, :, 2].astype(np.int16) + 70, 0, 255).astype(np.uint8)
        verdict = self.verdict(candidate)
        self.assertFalse(verdict["accepted"])
        self.assertIn("colour_drift", verdict["reasons"])

    def test_a_denoise_must_remove_noise_without_flattening_detail(self):
        noisy = portrait(noise=22)
        report = self.analyzer.analyze(noisy)
        validator = FinalImageQualityValidator(self.analyzer, report)
        good = cv2.fastNlMeansDenoisingColored(noisy, None, 10, 10, 7, 21)
        flat = cv2.GaussianBlur(noisy, (0, 0), 6.0)
        accepted = validator.evaluate(noisy, good, "denoising", report)
        flattened = validator.evaluate(noisy, flat, "denoising", report)
        self.assertTrue(accepted["accepted"], accepted["reasons"])
        self.assertFalse(flattened["accepted"])

    def test_sharpening_with_halos_is_rejected(self):
        soft = cv2.GaussianBlur(portrait(), (0, 0), 3.0)
        report = self.analyzer.analyze(soft)
        validator = FinalImageQualityValidator(self.analyzer, report)
        mild = cv2.addWeighted(soft, 1.6, cv2.GaussianBlur(soft, (0, 0), 3.0), -0.6, 0)
        harsh = cv2.addWeighted(soft, 6.0, cv2.GaussianBlur(soft, (0, 0), 1.5), -5.0, 0)
        self.assertFalse(validator.evaluate(soft, harsh, "motion_deblurring", report)["accepted"])
        self.assertTrue(validator.evaluate(soft, mild, "motion_deblurring", report)["gates"]["artifacts"]["passed"])

    def test_an_unverified_generative_face_restoration_is_never_accepted(self):
        verdict = self.verdict(self.parent.copy(), operation="face_restoration")
        self.assertFalse(verdict["accepted"])
        self.assertIn("identity_unverifiable", verdict["reasons"])

    def test_a_failed_identity_check_rejects_even_a_good_looking_candidate(self):
        class Fails:
            def check(self, candidate):
                return {"status": "failed", "similarity": 0.31, "threshold": 0.7}

        validator = FinalImageQualityValidator(self.analyzer, self.report, identity=Fails())
        verdict = validator.evaluate(self.parent, self.lifted(5.0), "low_light_enhancement", self.report)
        self.assertFalse(verdict["accepted"])
        self.assertIn("identity_changed", verdict["reasons"])

    def test_a_lost_face_rejects_the_candidate(self):
        faceless = ImageQualityAnalyzer(detector=StubDetector([]))
        validator = FinalImageQualityValidator(faceless, self.report)
        verdict = validator.evaluate(self.parent, self.lifted(5.0), "low_light_enhancement", self.report)
        self.assertIn("face_lost", verdict["reasons"])

    def test_preference_favours_the_smaller_change_for_the_same_gain(self):
        gentle = {"improvement": 0.6, "change_cost": 0.10}
        heavy = {"improvement": 0.6, "change_cost": 0.30}
        self.assertGreater(FinalImageQualityValidator.preference(gentle), FinalImageQualityValidator.preference(heavy))

    def test_reference_metrics_compare_against_a_clean_image(self):
        clean = portrait()
        blurred = cv2.GaussianBlur(clean, (0, 0), 2.0)
        metrics = reference_metrics(clean, blurred)
        self.assertGreater(metrics["psnr"], 15)
        self.assertLess(metrics["ssim"], 1.0)
        self.assertFalse(metrics["resized_to_reference"])
        self.assertTrue(reference_metrics(clean, cv2.resize(clean, (200, 150)))["resized_to_reference"])
        self.assertEqual(Limits().consistency_psnr_min, 27.0)


class CandidatePoolTests(SimpleTestCase):
    def test_the_pool_is_bounded_and_records_each_candidate(self):
        original = portrait()
        pool = CandidatePool(original, max_candidates=3)
        first = pool.add(original.copy(), pool.original, "denoising", "blend 1.0", [{"model": "x"}])
        pool.add(original.copy(), first, "motion_deblurring")
        with self.assertRaises(PoolFull):
            pool.add(original.copy(), first, "super_resolution")
        pool.select(first)
        described = pool.describe()
        self.assertEqual([item["selected"] for item in described], [False, True, False])
        self.assertEqual(described[1]["models"], [{"model": "x"}])
        self.assertNotIn("image", described[0])


def runner_lift(image, engine, variant):
    return cv2.convertScaleAbs(image, alpha=1.0 + 4.0 * variant["blend"], beta=0), [{"model": engine, "device": "stand-in"}]


def runner_denoise(image, engine, variant):
    return cv2.fastNlMeansDenoisingColored(image, None, 10, 10, 7, 21), [{"model": engine, "device": "stand-in"}]


@override_settings(**ALL_ON)
class ServiceTests(SimpleTestCase):
    def service(self, face=True, runners=None, **config):
        analyzer = analyzer_with() if face else analyzer_with(None)
        return AdaptiveImageEnhancementService(analyzer=analyzer, config=EngineConfig(**config), runners=runners or {})

    def run_service(self, image, service=None, runners=None, **kwargs):
        service = service or self.service(runners=runners)
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            return service.enhance(image, **kwargs)

    def test_a_clean_photograph_is_returned_unchanged_and_not_called_enhanced(self):
        source = portrait()
        output, _level, parameters = self.run_service(source, runners={ops.LOW_LIGHT: runner_lift})
        np.testing.assert_array_equal(output, source)
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [])
        self.assertIn("did not need", parameters["adaptive"]["summary"])
        self.assertEqual(parameters["algorithm"], "adaptive_v1+original_preserved")

    def test_a_dark_subject_is_lifted_and_the_conservative_variant_can_win(self):
        source = portrait(brightness=0.12)
        output, _level, parameters = self.run_service(source, runners={ops.LOW_LIGHT: runner_lift})
        self.assertGreater(float(output.mean()), float(source.mean()) + 8)
        adaptive = parameters["adaptive"]
        self.assertIn(adaptive["outcome"], (ENHANCED, PARTIALLY_ENHANCED))
        self.assertEqual(adaptive["operations_applied"], [ops.LOW_LIGHT])
        stage = next(s for s in adaptive["stages"] if s["operation"] == ops.LOW_LIGHT)
        self.assertEqual(stage["status"], "accepted")
        self.assertEqual(len(stage["candidates"]), 2)  # both variants were proposed and judged

    def test_only_the_operations_the_defects_call_for_run(self):
        calls = []

        def spy(name, runner):
            def run(image, engine, variant):
                calls.append(name)
                return runner(image, engine, variant)
            return run

        source = portrait(brightness=0.12)
        self.run_service(source, runners={ops.LOW_LIGHT: spy("lift", runner_lift), ops.DENOISE: spy("denoise", runner_denoise),
                                          ops.DEBLUR: spy("deblur", runner_lift)})
        self.assertNotIn("deblur", calls)
        self.assertTrue(set(calls) <= {"lift", "denoise"})

    def test_an_unsafe_candidate_is_rejected_and_the_original_is_kept(self):
        def over_exposed(image, engine, variant):
            return np.clip(image.astype(np.float32) * 25 + 90, 0, 255).astype(np.uint8), [{"model": engine}]

        source = portrait(brightness=0.12)
        output, _level, parameters = self.run_service(source, runners={ops.LOW_LIGHT: over_exposed})
        np.testing.assert_array_equal(output, source)
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        stage = parameters["adaptive"]["stages"][0]
        self.assertEqual(stage["status"], "rejected")
        self.assertTrue(stage["rejection_reasons"])
        self.assertIn("kept", parameters["adaptive"]["summary"])

    def test_a_model_that_cannot_run_is_recorded_and_the_classical_engine_takes_over(self):
        from .lowlight import LowLightUnavailable

        def run(image, engine, variant):
            if engine == "retinexformer":
                raise LowLightUnavailable("checkpoint unavailable")
            return runner_lift(image, engine, variant)

        _output, _level, parameters = self.run_service(portrait(brightness=0.12), runners={ops.LOW_LIGHT: run})
        stage = parameters["adaptive"]["stages"][0]
        self.assertEqual(stage["skipped_engines"][0]["engine"], "retinexformer")
        self.assertEqual(stage["status"], "accepted")
        self.assertIn("opencv_exposure", parameters["algorithm"])

    def test_defocus_blur_is_reported_as_unresolved_not_hidden(self):
        source = cv2.GaussianBlur(resized(dead_leaves(), 900), (0, 0), 3.5)
        output, _level, parameters = self.run_service(source, service=self.service(face=False), runners={})
        np.testing.assert_array_equal(output, source)
        adaptive = parameters["adaptive"]
        self.assertEqual(adaptive["outcome"], ORIGINAL_PRESERVED)
        self.assertTrue(any("defocus" in item["reason"].lower() or "blur" in item["reason"].lower() for item in adaptive["operations_skipped"]))

    def test_the_candidate_limit_bounds_the_work(self):
        source = portrait(brightness=0.12, noise=22)
        _out, _level, parameters = self.run_service(
            source, service=self.service(max_candidates=3, runners={ops.LOW_LIGHT: runner_lift, ops.DENOISE: runner_denoise}),
        )
        self.assertLessEqual(len(parameters["adaptive"]["candidates"]), 3)

    def test_the_time_budget_stops_new_operations(self):
        source = portrait(brightness=0.12)
        _out, _level, parameters = self.run_service(
            source, service=self.service(time_budget_seconds=-1.0, runners={ops.LOW_LIGHT: runner_lift}),
        )
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        self.assertTrue(any("time budget" in warning for warning in parameters["warnings"]))

    def test_face_restoration_is_never_applied_without_a_verifiable_identity(self):
        calls = []

        def restore(image, engine, variant):
            calls.append(1)
            return image, [{"model": engine}]

        blurry_face = portrait(face_box=(150, 100, 180, 220))
        x, y, w, h = (150, 100, 180, 220)
        blurry_face[y:y + h, x:x + w] = cv2.GaussianBlur(blurry_face[y:y + h, x:x + w], (0, 0), 6.0)
        _out, _level, parameters = self.run_service(blurry_face, runners={ops.FACE: restore})
        self.assertEqual(calls, [])
        self.assertTrue(any(item["operation"] == ops.FACE for item in parameters["adaptive"]["operations_skipped"]))

    def test_no_biometric_permission_means_no_face_restoration_and_no_embedding(self):
        _out, _level, parameters = self.run_service(portrait(), allow_biometric_processing=False)
        self.assertTrue(any("not permitted" in item["reason"] for item in parameters["adaptive"]["operations_skipped"]))
        self.assertEqual(parameters["identity_status"], "unavailable")

    def test_the_parameters_keep_the_shape_the_job_and_api_code_read(self):
        _out, level, parameters = self.run_service(portrait(brightness=0.12), runners={ops.LOW_LIGHT: runner_lift})
        for key in ("algorithm", "quality_before", "quality_after", "quality_comparison", "warnings", "identity_status",
                    "identity_verification", "stage_reports", "resolution_contract", "render_policy", "pipeline_version"):
            self.assertIn(key, parameters)
        self.assertIn(level, ("low", "medium", "high"))
        self.assertEqual(parameters["render_policy"]["name"], "adaptive_v1")

    def test_the_source_array_is_never_modified(self):
        source = portrait(brightness=0.12)
        before = source.copy()
        self.run_service(source, runners={ops.LOW_LIGHT: runner_lift})
        np.testing.assert_array_equal(source, before)

    def test_cancellation_is_honoured_between_operations(self):
        class Cancelled(Exception):
            pass

        def checkpoint(name):
            if name == ops.LOW_LIGHT:
                raise Cancelled()

        with self.assertRaises(Cancelled):
            self.run_service(portrait(brightness=0.12), runners={ops.LOW_LIGHT: runner_lift}, checkpoint=checkpoint)

    def test_enhance_bgr_dispatches_to_the_engine_under_the_adaptive_policy(self):
        from .services import enhance_bgr

        with patch("enhancement.adaptive.service.AdaptiveImageEnhancementService.enhance",
                   return_value=("image", "low", {"adaptive": {}})) as engine:
            result = enhance_bgr(portrait(), "auto")
        engine.assert_called_once()
        self.assertEqual(result[0], "image")

    @override_settings(ADAPTIVE_DEBUG_ARTIFACTS=True)
    def test_debug_mode_saves_only_the_stages_that_ran_and_the_candidate_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            from django.core.files.storage import default_storage

            _out, _level, parameters = self.run_service(portrait(brightness=0.12), runners={ops.LOW_LIGHT: runner_lift})
            location = parameters["adaptive"]["debug_artifacts"]
            _dirs, files = default_storage.listdir(location.rstrip("/"))
        self.assertIn("original.png", files)
        self.assertIn("01_quality_analysis.json", files)
        self.assertIn("02_low_light.png", files)
        self.assertIn("07_final.png", files)
        self.assertIn("candidates.json", files)
        self.assertNotIn("04_deblurred.png", files)
        self.assertNotIn("06_upscaled.png", files)

    def test_debug_artifacts_are_off_by_default_and_expire(self):
        import tempfile

        from .adaptive import debug

        _out, _level, parameters = self.run_service(portrait(brightness=0.12), runners={ops.LOW_LIGHT: runner_lift})
        self.assertIsNone(parameters["adaptive"]["debug_artifacts"])
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            recorder = debug.DebugRecorder(True, "run1")
            recorder.json("x", {"a": 1})
            self.assertEqual(debug.cleanup(older_than_hours=24), 0)
            self.assertEqual(debug.cleanup(older_than_hours=-1), 1)


def stub_analyzer_service():
    """The engine as the job code builds it (no injected runners), with a stand-in face detector."""
    from .adaptive.service import AdaptiveImageEnhancementService as Service

    original = Service._analyzer

    def analyzer(self):
        self.analyzer = analyzer_with()
        return self.analyzer

    return patch.object(Service, "_analyzer", analyzer), original


@override_settings(**ALL_ON, MEDIA_ROOT=__import__("tempfile").mkdtemp(), CELERY_TASK_ALWAYS_EAGER=True)
class AdaptiveJobTests(__import__("enhancement.test_pipeline_regressions", fromlist=["ResultFixture"]).ResultFixture):
    """The adaptive engine behind the real job code: claim, publish, API fields, downloads."""

    def make_from(self, image, name="job.jpg"):
        from django.core.files.base import ContentFile

        from uploads.models import ImagePreprocessingResult, ImageUpload

        from .models import EnhancementLevel, ImageEnhancementResult

        encoded = cv2.imencode(".png", image)[1].tobytes()
        upload = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name=name, file_extension=".png",
                                            mime_type="image/png", file_size=len(encoded), width=image.shape[1],
                                            height=image.shape[0], status="uploaded")
        upload.original_file.save(name, ContentFile(encoded))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=upload, status="completed", output_format="png", output_name=name,
            output_width=image.shape[1], output_height=image.shape[0], processed_file_size=len(encoded))
        preprocessing.processed_file.save(name, ContentFile(encoded))
        return ImageEnhancementResult.objects.create(owner=self.user, image=upload, preprocessing_result=preprocessing,
                                                     status="queued", requested_level=EnhancementLevel.AUTO)

    def process(self, image, runners=None):
        from .services import process_enhancement_result

        result = self.make_from(image)
        from contextlib import ExitStack

        with ExitStack() as stack:
            stack.enter_context(stub_analyzer_service()[0])
            checker = stack.enter_context(patch("enhancement.adaptive.service.IdentityChecker"))
            for name, replacement in (runners or {}).items():
                stack.enter_context(patch(f"enhancement.adaptive.operations.{name}", replacement))
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured."
            return process_enhancement_result(result)

    def test_a_clean_photograph_is_stored_unchanged_and_reported_as_preserved(self):
        from rest_framework.test import APIClient

        source = portrait()
        result = self.process(source)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.applied_parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        client = APIClient()
        client.force_authenticate(self.user)
        body = client.get(f"/api/enhancements/results/{result.pk}/").json()
        self.assertEqual(body["enhancement_outcome"], "original_preserved")
        self.assertIn("did not need", body["enhancement_summary"])
        self.assertEqual((body["output_width"], body["output_height"]), (source.shape[1], source.shape[0]))
        download = client.get(f"/api/enhancements/results/{result.pk}/download/")
        self.assertEqual(download.status_code, 200)
        decoded = cv2.imdecode(np.frombuffer(b"".join(download.streaming_content), np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(decoded, source)

    def test_a_dark_photograph_gets_one_enhanced_file_and_a_plain_summary(self):
        from rest_framework.test import APIClient

        source = portrait(brightness=0.12)
        result = self.process(source, runners={"run_low_light": lambda image, engine, blend: (cv2.convertScaleAbs(image, alpha=1 + 4 * blend), [{"model": engine}])})
        self.assertEqual(result.status, "completed")
        client = APIClient()
        client.force_authenticate(self.user)
        body = client.get(f"/api/enhancements/results/{result.pk}/").json()
        self.assertIn(body["enhancement_outcome"], ("enhanced", "partially_enhanced"))
        self.assertTrue(body["download_url"])
        self.assertIn("brightened", body["enhancement_summary"])
        # one final image only: the intermediate candidates are not stored as files in production mode
        self.assertEqual(result.applied_parameters["adaptive"]["debug_artifacts"], None)

    def test_a_job_that_fails_reports_failed_and_keeps_no_output(self):
        from rest_framework.exceptions import ValidationError
        from rest_framework.test import APIClient

        from .services import process_enhancement_result

        result = self.make_from(portrait())
        with patch("enhancement.adaptive.service.AdaptiveImageEnhancementService.enhance", side_effect=RuntimeError("boom")):
            with self.assertRaises(ValidationError):
                process_enhancement_result(result)
        client = APIClient()
        client.force_authenticate(self.user)
        body = client.get(f"/api/enhancements/results/{result.pk}/").json()
        self.assertEqual((body["status"], body["enhancement_outcome"]), ("failed", "failed"))
        self.assertIsNone(body["download_url"])

    def test_another_user_cannot_see_or_download_the_result(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        result = self.process(portrait())
        other = get_user_model().objects.create_user(username="other", email="o@example.com", password="ForestRiver!4821",
                                                     is_email_verified=True)
        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(client.get(f"/api/enhancements/results/{result.pk}/").status_code, 404)
        self.assertEqual(client.get(f"/api/enhancements/results/{result.pk}/download/").status_code, 404)

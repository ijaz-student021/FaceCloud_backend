"""The polish operation of the adaptive engine (ADAPTIVE_POLISH_ENABLED): a gentle, bounded, verified improvement for a photograph on which
nothing else was found, and, just as important, the proof that with the flag off (or absent) and on every photograph that has a defect,
nothing changes by one pixel.

As elsewhere in the adaptive tests, models are replaced by stand-ins: these are tests of decisions and of what is measured. Real-model
behaviour on real photographs is in docs/evaluation/ (the golden before/after run) and in test_adaptive_real_models.py.
"""
import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from validation.services import FaceDetection

from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import ORIGINAL_PRESERVED, AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator, Limits, landmark_shift, local_contrast, mean_delta_e
from .restoration import POLISH_REFUSED_REASON, describe_stages
from .test_adaptive_engine import ALL_ON, analyzer_with, landmarks_for, portrait, runner_denoise, runner_lift

FACE = (150, 100, 180, 220)


class SkinDetector:
    """A face "detector" that really looks at the pixels: the box of the skin-toned patch, so the landmarks move if the pixels change.

    (The shared StubDetector returns fixed landmarks for any image, which could never show drift.)
    """

    def detect(self, image):
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        skin = cv2.inRange(hsv, (3, 35, 60), (30, 200, 255))
        skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(skin)
        if count < 2:
            return FaceDetection(boxes=[], scores=[], landmarks=[], landmark_scores=[], detector="skin", detector_version="1",
                                 confidence_type="stub")
        index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, w, h = (int(v) for v in stats[index, :4])
        return FaceDetection(boxes=[(x, y, w, h)], scores=[0.99], landmarks=[landmarks_for((x, y, w, h))], landmark_scores=[0.99],
                             detector="skin", detector_version="1", confidence_type="stub")


def skin_analyzer():
    return ImageQualityAnalyzer(detector=SkinDetector())


def soft_portrait():
    """A well-lit portrait with nothing the analyzer calls a defect, a little soft and flat: what a gentle polish is for."""
    image = portrait()
    image = cv2.GaussianBlur(image, (0, 0), 0.9)
    flat = image.astype(np.float32)
    return np.clip(flat.mean() + (flat - flat.mean()) * 0.82, 0, 255).astype(np.uint8)


def crisp_balanced_image():
    """Already balanced and sharp: a full-range ramp with hard black and white squares. Nothing for a polish to improve."""
    ramp = np.tile(np.linspace(0, 255, 640, dtype=np.float32), (480, 1))
    image = np.repeat(ramp[..., None], 3, axis=2)
    for row in range(40, 440, 80):
        for column in range(40, 600, 80):
            image[row:row + 40, column:column + 40] = 0.0 if (row // 80 + column // 80) % 2 else 255.0
    return image.astype(np.uint8)


class FakeIdentity:
    """An identity checker whose "embedding" is the face crop shrunk to 16 x 16: unchanged pixels give 1.0, a changed face gives less."""

    available, threshold, reason = True, 0.9, ""

    def __init__(self, original, allowed=True):
        self.reference = self._vector(original)

    @staticmethod
    def _vector(image):
        detection = SkinDetector().detect(image)
        x, y, w, h = detection.boxes[0]
        crop = cv2.resize(cv2.cvtColor(image[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA)
        vector = crop.astype(np.float32).ravel() - float(crop.mean())
        return vector / max(float(np.linalg.norm(vector)), 1e-6)

    def check(self, candidate):
        similarity = float(np.dot(self.reference, self._vector(candidate)))
        return {"status": "verified" if similarity >= self.threshold else "failed", "similarity": round(similarity, 4),
                "threshold": self.threshold}


def run_engine(image, polish, style="natural", runners=None, analyzer=None, identity=True):
    """The engine as the job code builds it, with stand-in models, a pixel-reading face detector and a stand-in identity check."""
    from unittest.mock import patch

    service = AdaptiveImageEnhancementService(analyzer=analyzer or skin_analyzer(), config=EngineConfig(), runners=runners or {})
    with override_settings(**ALL_ON, ADAPTIVE_POLISH_ENABLED=polish), patch(
        "enhancement.adaptive.service.IdentityChecker", FakeIdentity if identity else _no_identity
    ):
        return service.enhance(image, style=style)


def _no_identity(original, allowed=True):
    from unittest.mock import MagicMock

    checker = MagicMock()
    checker.available, checker.threshold, checker.reason = False, None, "No identity model is configured, so identity was not checked."
    return checker


class PolishSettingTests(SimpleTestCase):
    def test_the_flag_is_off_by_default(self):
        self.assertIs(settings.ADAPTIVE_POLISH_ENABLED, False)

    def test_polish_is_the_last_operation_and_has_one_engine(self):
        self.assertEqual(ops.ORDER[-2:], (ops.POLISH, ops.COLORIZE))      # polish is the last *defect-free* operation; colourization (black and white only) follows it
        self.assertEqual(ops.ENGINES[ops.POLISH], ("opencv_polish",))


@override_settings(**ALL_ON)
class PolishRouterTests(SimpleTestCase):
    def router(self):
        return AdaptiveEnhancementRouter(EngineConfig())

    def report(self, image=None, face=True):
        return (analyzer_with() if face else analyzer_with(None)).analyze(image if image is not None else portrait())

    def test_the_default_call_never_offers_polish(self):
        self.assertIsNone(self.router().next_step(self.report(), set()).step)

    def test_a_clean_photograph_is_offered_polish_only_when_asked(self):
        decision = self.router().next_step(self.report(), set(), polish={"style": "natural"})
        self.assertEqual(decision.step.operation, ops.POLISH)
        self.assertEqual([variant["name"] for variant in decision.step.variants], ["blend 1.0", "blend 0.65"])
        self.assertEqual(decision.step.engines, ["opencv_polish"])

    def test_the_styles_set_the_strengths_and_ai_max_is_never_polished(self):
        natural = self.router().next_step(self.report(), set(), polish={"style": "natural"}).step.variants[0]
        dynamic = self.router().next_step(self.report(), set(), polish={"style": "dynamic"}).step.variants[0]
        self.assertEqual((natural["clahe"], natural["amount"]), (0.25, 0.25))
        self.assertEqual((dynamic["clahe"], dynamic["amount"]), (0.55, 0.50))
        self.assertIsNone(self.router().next_step(self.report(), set(), polish={"style": "ai_max"}).step)

    def test_a_dark_photograph_gets_the_low_light_logic_not_polish(self):
        report = self.report(portrait(brightness=0.12))
        self.assertEqual(self.router().next_step(report, set(), polish={"style": "natural"}).step.operation, ops.LOW_LIGHT)
        after_lift = self.router().next_step(report, {ops.LOW_LIGHT}, polish={"style": "natural"}).step
        self.assertTrue(after_lift is None or after_lift.operation != ops.POLISH)

    def test_a_dark_scene_left_as_photographed_is_not_polished(self):
        scene = portrait(brightness=0.35)
        x, y, w, h = FACE
        scene[y:y + h, x:x + w] = np.clip(portrait()[y:y + h, x:x + w].astype(np.float32) * 1.05, 0, 255).astype(np.uint8)
        report = self.report(scene)
        self.assertEqual(report.exposure["status"], "dark_scene")
        # (the dark scene itself is lifted first, whatever the style; polish is what is not offered to it, with the lift attempted)
        self.assertIsNone(self.router().next_step(report, {ops.LOW_LIGHT}, polish={"style": "natural"}).step)

    def test_a_photograph_with_a_recommended_operation_is_not_polished(self):
        report = self.report(portrait(noise=25.0))
        self.assertIn("denoising", report.recommended_operations)
        step = self.router().next_step(report, {ops.DENOISE}, polish={"style": "natural"}).step
        self.assertIsNone(step)


class PolishOperationTests(SimpleTestCase):
    VARIANT = {"name": "blend 1.0", "blend": 1.0, "clahe": 0.25, "amount": 0.25, "faces": []}

    def test_it_keeps_size_and_type_and_is_deterministic(self):
        image = soft_portrait()
        first, models = ops.run_polish(image, "opencv_polish", self.VARIANT)
        second, _ = ops.run_polish(image, "opencv_polish", self.VARIANT)
        self.assertEqual((first.shape, first.dtype), (image.shape, np.uint8))
        np.testing.assert_array_equal(first, second)
        self.assertEqual(models[0]["model"], "opencv_polish")

    def test_the_source_is_not_modified_and_the_change_is_small(self):
        image = soft_portrait()
        before = image.copy()
        out, _ = ops.run_polish(image, "opencv_polish", self.VARIANT)
        np.testing.assert_array_equal(image, before)
        self.assertGreater(float(np.abs(out.astype(int) - image.astype(int)).mean()), 0.0)
        self.assertLessEqual(mean_delta_e(image, out), 3.0)

    def test_a_smaller_blend_changes_less(self):
        image = soft_portrait()
        full, _ = ops.run_polish(image, "opencv_polish", self.VARIANT)
        light, _ = ops.run_polish(image, "opencv_polish", {**self.VARIANT, "blend": 0.65})
        self.assertLess(mean_delta_e(image, light), mean_delta_e(image, full))

    def test_the_result_does_not_depend_on_the_strip_size(self):
        """Large photographs are processed in overlapping strips; the strips must not show."""
        base = soft_portrait()
        image = np.clip(base.astype(np.int16) + np.random.default_rng(5).integers(-2, 3, base.shape), 0, 255).astype(np.uint8)
        whole, _ = ops.run_polish(image, "opencv_polish", self.VARIANT)
        from unittest.mock import patch

        with patch.object(ops, "_POLISH_STRIP_ROWS", 97):
            strips, _ = ops.run_polish(image, "opencv_polish", self.VARIANT)
        self.assertLessEqual(int(np.abs(whole.astype(int) - strips.astype(int)).max()), 1)

    def test_an_already_sharp_face_is_not_sharpened_but_a_soft_one_is(self):
        image = soft_portrait()
        sharp_face = [[*FACE, 260.0]]
        soft_face = [[*FACE, 60.0]]
        x, y, w, h = FACE
        sharp_out, _ = ops.run_polish(image, "opencv_polish", {**self.VARIANT, "clahe": 0.0, "faces": sharp_face})
        soft_out, _ = ops.run_polish(image, "opencv_polish", {**self.VARIANT, "clahe": 0.0, "faces": soft_face})
        centre = (slice(y + 40, y + h - 40), slice(x + 30, x + w - 30))

        def fine_detail(out):   # how much of the face is fine texture (what an unsharp mask adds), whatever the tone or colour changes
            gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY).astype(np.float32)
            return float(np.abs(gray - cv2.GaussianBlur(gray, (0, 0), 1.2))[centre].mean())

        self.assertLess(fine_detail(sharp_out), fine_detail(soft_out))
        # all an already-sharp face gets is the frame's levels stretch (at most 8 percent), no unsharp mask
        self.assertLess(fine_detail(sharp_out), fine_detail(image) * 1.12)


@override_settings(**ALL_ON)
class PolishEngineTests(SimpleTestCase):
    def test_a_clean_portrait_is_polished_and_stays_the_same_photograph(self):
        source = soft_portrait()
        before = skin_analyzer().analyze(source)
        self.assertEqual(before.recommended_operations, [])
        self.assertEqual(before.exposure["status"], "normal")
        out, _level, parameters = run_engine(source, polish=True)
        adaptive = parameters["adaptive"]
        self.assertEqual(adaptive["operations_applied"], [ops.POLISH])
        self.assertEqual(adaptive["outcome"], "enhanced")
        described = describe_stages(parameters)
        self.assertEqual(described["applied_stages"][0]["stage"], "polish")
        self.assertEqual(described["applied_stages"][0]["engine"], "opencv_polish")
        self.assertIn(described["applied_stages"][0]["variant"], ("blend 1.0", "blend 0.65"))
        self.assertEqual(adaptive["summary"], "Polished: balanced the contrast and sharpened lightly.")
        self.assertEqual(parameters["algorithm"], "adaptive_v1+opencv_polish")
        after = skin_analyzer().analyze(out)
        drift = landmark_shift(before.faces, after.faces)
        self.assertIsNotNone(drift)
        self.assertLess(drift, 0.015)                     # under 1.5 percent of the face width
        self.assertLessEqual(mean_delta_e(source, out), 3.0)
        identity = parameters["identity_verification"]
        self.assertEqual(identity["state"], "verified")
        self.assertGreaterEqual(identity["similarity"], 0.99)   # the identity score of the original against itself is 1.0
        self.assertGreater(float(np.abs(out.astype(int) - source.astype(int)).mean()), 0.0)

    def test_an_already_sharp_and_balanced_photograph_is_returned_as_it_is_with_the_reason(self):
        source = crisp_balanced_image()
        out, _level, parameters = run_engine(source, polish=True, analyzer=analyzer_with(None), identity=False)
        np.testing.assert_array_equal(out, source)
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [])
        self.assertEqual(describe_stages(parameters)["skipped_stages"], [{"stage": "polish", "reason": POLISH_REFUSED_REASON}])
        self.assertEqual(POLISH_REFUSED_REASON, "Already balanced and sharp: nothing to improve.")
        self.assertIn("did not need", parameters["adaptive"]["summary"])      # not "failed the safety checks"
        self.assertIsNone(describe_stages(parameters)["hd_unavailable_reason"])
        refused = [stage for stage in parameters["adaptive"]["stages"] if stage["operation"] == ops.POLISH]
        self.assertEqual(refused[0]["status"], "rejected")
        self.assertIn("no_measurable_improvement", refused[0]["rejection_reasons"])

    def test_a_dark_photograph_gets_low_light_and_polish_does_not_run(self):
        source = portrait(brightness=0.12)
        _out, _level, parameters = run_engine(source, polish=True, runners={ops.LOW_LIGHT: runner_lift}, identity=False)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.LOW_LIGHT])
        self.assertFalse([stage for stage in parameters["adaptive"]["stages"] if stage["operation"] == ops.POLISH])
        self.assertNotIn("polish", [stage["stage"] for stage in describe_stages(parameters)["applied_stages"]])

    def test_a_noisy_photograph_gets_denoise_and_polish_does_not_run(self):
        source = portrait(noise=25.0)
        _out, _level, parameters = run_engine(source, polish=True, runners={ops.DENOISE: runner_denoise}, analyzer=analyzer_with(), identity=False)
        self.assertIn(ops.DENOISE, parameters["adaptive"]["operations_applied"])
        self.assertNotIn(ops.POLISH, parameters["adaptive"]["operations_applied"])
        self.assertFalse([stage for stage in parameters["adaptive"]["stages"] if stage["operation"] == ops.POLISH])

    def test_polish_is_never_stacked_on_a_rejected_attempt_either(self):
        source = portrait(brightness=0.12)
        _out, _level, parameters = run_engine(source, polish=True, runners={ops.LOW_LIGHT: lambda image, engine, variant: (image, [{"model": engine}])},
                                              identity=False)
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        self.assertFalse([stage for stage in parameters["adaptive"]["stages"] if stage["operation"] == ops.POLISH])

    def test_ai_max_polishes_as_dynamic_does_with_its_stronger_share_since_the_profiles(self):
        """Before enhancement/adaptive/profile.py the strongest style was never polished (it ignored the style); now it polishes at the dynamic strengths x1.25."""
        self.assertEqual(profile_for("ai_max").polish_style, "dynamic")

    def test_the_flag_off_restores_todays_behaviour_exactly(self):
        for label, source, runners in (("soft portrait", soft_portrait(), None), ("crisp", crisp_balanced_image(), None)):
            with self.subTest(label):
                off = run_engine(source, polish=False, runners=runners, identity=False)
                with override_settings(**ALL_ON):   # the flag absent altogether: the default
                    absent_service = AdaptiveImageEnhancementService(analyzer=skin_analyzer(), config=EngineConfig())
                    from unittest.mock import patch

                    with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
                        absent = absent_service.enhance(source)
                np.testing.assert_array_equal(off[0], source)
                np.testing.assert_array_equal(absent[0], source)
                self.assertEqual(off[2]["adaptive"]["outcome"], ORIGINAL_PRESERVED)
                self.assertEqual(off[2]["adaptive"]["summary"], absent[2]["adaptive"]["summary"])
                self.assertEqual(describe_stages(off[2]), describe_stages(absent[2]))
                self.assertEqual(describe_stages(off[2])["skipped_stages"], [])

    def test_with_the_flag_on_a_dark_and_a_noisy_photograph_are_identical_to_the_flag_off(self):
        cases = (("dark", portrait(brightness=0.12), {ops.LOW_LIGHT: runner_lift}),
                 ("noisy", portrait(noise=25.0), {ops.DENOISE: runner_denoise}))
        for label, source, runners in cases:
            with self.subTest(label):
                on = run_engine(source, polish=True, runners=runners, analyzer=analyzer_with(), identity=False)
                off = run_engine(source, polish=False, runners=runners, analyzer=analyzer_with(), identity=False)
                np.testing.assert_array_equal(on[0], off[0])
                self.assertEqual(on[2]["adaptive"]["operations_applied"], off[2]["adaptive"]["operations_applied"])
                self.assertEqual(on[2]["adaptive"]["outcome"], off[2]["adaptive"]["outcome"])
                self.assertEqual(describe_stages(on[2]), describe_stages(off[2]))

    def test_a_stored_result_from_before_polish_describes_itself_unchanged(self):
        """describe_stages (what GET /api/enhancements/results/<id>/ returns) adds nothing for a result that never had a polish stage."""
        _out, _level, parameters = run_engine(portrait(brightness=0.12), polish=False, runners={ops.LOW_LIGHT: runner_lift}, identity=False)
        described = describe_stages(parameters)
        self.assertEqual([stage["stage"] for stage in described["applied_stages"]], ["low_light"])
        self.assertEqual(described["skipped_stages"], [])
        self.assertNotIn("polish", str(parameters["adaptive"]["stages"]))


@override_settings(**ALL_ON)
class PolishValidatorTests(SimpleTestCase):
    def setUp(self):
        self.analyzer = analyzer_with(None)
        self.parent = soft_portrait()
        self.report = self.analyzer.analyze(self.parent)
        self.validator = FinalImageQualityValidator(self.analyzer, self.report)

    def verdict(self, candidate):
        return self.validator.evaluate(self.parent, candidate, ops.POLISH, self.report)

    def test_the_same_image_has_nothing_to_improve(self):
        verdict = self.verdict(self.parent.copy())
        self.assertFalse(verdict["accepted"])
        self.assertIn("no_measurable_improvement", verdict["reasons"])

    def test_a_visibly_different_photograph_is_refused(self):
        brighter = np.clip(self.parent.astype(np.float32) * 1.35 + 20, 0, 255).astype(np.uint8)
        verdict = self.verdict(brighter)
        self.assertFalse(verdict["accepted"])
        self.assertIn("photograph_changed_too_much", verdict["reasons"])

    def test_more_noise_is_refused(self):
        noisy = np.clip(self.parent.astype(np.float32) + np.random.default_rng(2).normal(0, 9, self.parent.shape), 0, 255).astype(np.uint8)
        self.assertIn("noise_increased", self.verdict(noisy)["reasons"])

    def test_a_polish_that_loses_contrast_is_refused_as_worse(self):
        flatter = np.clip(self.parent.astype(np.float32).mean() + (self.parent.astype(np.float32) - self.parent.mean()) * 0.9, 0, 255).astype(np.uint8)
        verdict = self.verdict(flatter)
        self.assertFalse(verdict["accepted"])

    def test_the_limits_are_the_documented_ones(self):
        limits = Limits()
        self.assertEqual((limits.polish_min_gain, limits.polish_noise_growth_max, limits.polish_delta_e_max, limits.polish_clip_growth_max),
                         (1.05, 1.10, 3.0, 0.5))

    def test_local_contrast_rises_with_a_local_contrast_stretch(self):
        base = self.parent
        stretched, _ = ops.run_polish(base, "opencv_polish", {"blend": 1.0, "clahe": 0.55, "amount": 0.5, "faces": []})
        self.assertGreater(local_contrast(stretched), local_contrast(base))

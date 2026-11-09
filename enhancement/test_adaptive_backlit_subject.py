"""A back-lit subject (a person against a window, a bright sky or a sunset) is crushed in the shadows while the frame is bright, so no rule about the frame calls the photograph dark.

The analyzer reports ``subject_exposure.backlit`` for the MAIN face (crushed tonal range, dark next to the frame, bright frame); AI Enhance then proposes the low-light lift as a SHADOW
lift (the network's output is blended in only where the original is clearly darker than the frame's own median) and the validator judges it like any other candidate, with one more gate:
a lift that makes the blocks of a heavily compressed photograph visible is refused, and so are the weaker blends of the same photograph.
Models are replaced by the classical lift here; what is tested is the decision, the weights and what the result says. Synthetic photographs only: a real one belongs in the evaluation
(docs/evaluation/2026-11-05-backlit-subject-lift.md), not in the repository.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator
from .services import measure_compression_artifacts
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait

HEAD = (250, 110, 140, 180)         # x, y, width, height of the head in the synthetic scenes
CLASSICAL = dict(ALL_ON, RETINEXFORMER_ENABLED=False)       # no network is needed: the classical lift (gamma on lightness) stands in for it


BOTH_STRENGTHS_PASS = dict(subject=18, texture=20.0)         # the stronger AND the gentler shadow lift pass every gate when nothing shows blocks
SUBJECT_WITH_STRUCTURE = dict(subject=30, texture=20.0)      # the dark parts keep enough tonal range through JPEG quality 40 for a lift to pass every other gate


def jpeg(image, quality):
    """The photograph as a JPEG of that quality (40: the blocks of the compression show, as in a small photograph that went through a messenger)."""
    return cv2.imdecode(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])[1], cv2.IMREAD_COLOR)


def scene(sky=150, subject=22, head=HEAD, size=(480, 640), body=True, texture=14.0, seed=5):
    """A bright sky with a dark head and body: what a person against a window or a sunset looks like to the analyzer. The sky stays under the validator's median ceiling (175) and the
    dark parts have some structure and a little colour (a grey scene has no saturation for the colour gate to measure), or a lift of them could not be judged like a real one."""
    height, width = size
    blue_sky = np.array([sky + 25, sky, sky - 30], np.float32)                      # BGR: bluish
    image = np.clip(np.tile(blue_sky, (height, width, 1)) - np.linspace(0, 25, height, dtype=np.float32)[:, None, None], 0, 255)
    x, y, w, h = head
    warm = (subject - 8, subject, subject + 14)                                      # BGR: a warm dark
    if body:
        cv2.rectangle(image, (x - w // 2, y + h - 10), (x + w + w // 2, height), warm, -1)
    cv2.ellipse(image, (x + w // 2, y + h // 2), (w // 2, h // 2), 0, 0, 360, (warm[0] + 4, warm[1] + 4, warm[2] + 4), -1)
    rng = np.random.default_rng(seed)
    dark = (image.mean(axis=2) < 90)[:, :, None]
    return np.clip(image + rng.normal(0, texture, image.shape) * dark, 0, 255).astype(np.uint8)


def subject_of(image, head=HEAD):
    return analyzer_with(head).analyze(image).subject_exposure


class SubjectExposureTests(SimpleTestCase):
    def test_a_crushed_subject_in_a_bright_frame_is_back_lit(self):
        subject = subject_of(scene())
        self.assertTrue(subject["backlit"])
        self.assertLess(subject["subject_median"], 60)
        self.assertLess(subject["ratio"], 0.40)
        self.assertGreater(subject["frame_mean"], 100)

    def test_a_silhouetted_head_too_small_for_a_face_is_judged_the_same_way(self):
        self.assertTrue(subject_of(scene(head=(300, 120, 30, 40)), head=(300, 120, 30, 40))["backlit"])

    def test_a_well_exposed_portrait_is_not(self):
        self.assertFalse(subject_of(portrait(), head=(150, 100, 180, 220))["backlit"])

    def test_a_studio_portrait_on_white_is_not_even_though_its_histogram_has_two_peaks(self):
        image = np.full((480, 640, 3), 235, np.uint8)
        cv2.rectangle(image, (0, 250), (640, 480), (25, 25, 25), -1)                  # dark clothes: a dark half and a bright half, as in the library's studio portraits
        x, y, w, h = HEAD
        image[y:y + h, x:x + w] = np.random.default_rng(2).uniform(95, 170, (h, w, 1)).astype(np.uint8)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        self.assertGreater(float((gray < 64).mean()), 0.3)                            # what a histogram rule (dark share and bright share both over 0.3) would call back-lit
        self.assertGreater(float((gray > 192).mean()), 0.3)
        subject = subject_of(image)
        self.assertFalse(subject["backlit"])
        self.assertGreater(subject["subject_median"], 100)

    def test_a_dark_skinned_face_with_a_wide_tonal_range_is_not_crushed(self):
        image = np.full((480, 640, 3), 190, np.uint8)
        x, y, w, h = HEAD
        image[y:y + h, x:x + w] = np.random.default_rng(3).uniform(2, 90, (h, w, 1)).astype(np.uint8)       # median about 46, range about 80
        subject = subject_of(image)
        self.assertLess(subject["subject_median"], 60)
        self.assertLess(subject["ratio"], 0.40)
        self.assertFalse(subject["backlit"])         # the tonal range is not compressed: the face is dark, not lost

    def test_a_subject_that_is_not_much_darker_than_the_frame_is_not(self):
        subject = subject_of(scene(sky=100, subject=42))
        self.assertGreaterEqual(subject["ratio"], 0.40)
        self.assertFalse(subject["backlit"])

    def test_a_subject_that_is_dark_but_not_under_the_brightness_limit_is_not(self):
        subject = subject_of(scene(sky=250, subject=66, body=False))
        self.assertLess(subject["ratio"], 0.40)
        self.assertGreaterEqual(subject["subject_median"], 60)
        self.assertFalse(subject["backlit"])

    def test_a_dark_frame_is_the_low_light_rules_business(self):
        report = analyzer_with(HEAD).analyze(scene(sky=90, subject=10))
        self.assertEqual(report.exposure["status"], "low_light_subject")             # the existing rule lifts the whole frame of this one
        self.assertLess(report.exposure["scene_median"], 70)
        self.assertLess(report.subject_exposure["ratio"], 0.40)                      # every other condition is met: only the dark frame excludes it
        self.assertFalse(report.subject_exposure["backlit"])

    def test_no_face_means_no_judgement(self):
        report = analyzer_with(None).analyze(scene())
        self.assertEqual(report.subject_exposure, {"backlit": False, "judged_on": "no face"})

    def test_the_limits_are_settings(self):
        image = scene()
        for name, value in (("ADAPTIVE_BACKLIT_SUBJECT_BRIGHTNESS_MAX", 5.0), ("ADAPTIVE_BACKLIT_SUBJECT_MAX_RATIO", 0.05), ("ADAPTIVE_BACKLIT_MIN_SCENE_MEDIAN", 250.0)):
            with self.subTest(setting=name), override_settings(**{name: value}):
                self.assertFalse(subject_of(image)["backlit"])

    def test_the_report_carries_it_for_the_result(self):
        self.assertIn("subject_exposure", analyzer_with(HEAD).analyze(scene()).as_dict())


class ShadowWeightTests(SimpleTestCase):
    def test_the_weight_is_full_on_the_subject_and_zero_on_the_sky(self):
        weight = ops.shadow_weight(scene())
        self.assertGreater(float(weight[250:330, 280:350].mean()), 0.95)       # inside the head
        self.assertLess(float(weight[10:90, 20:600].max()), 0.02)              # the sky

    def test_it_is_relative_to_the_frame_a_darker_sky_is_still_background(self):
        weight = ops.shadow_weight(scene(sky=120, subject=15))
        self.assertLess(float(weight[10:90, 20:600].max()), 0.02)
        self.assertGreater(float(weight[250:330, 280:350].mean()), 0.95)

    def test_the_lift_leaves_the_sky_as_photographed_and_lifts_the_subject(self):
        image = scene()
        lifted, record = ops.run_low_light(image, "opencv_exposure", 1.0, "shadows")
        sky = (slice(10, 90), slice(20, 600))
        head = (slice(250, 330), slice(280, 350))
        self.assertLessEqual(int(np.abs(lifted[sky].astype(int) - image[sky].astype(int)).max()), 2)
        self.assertGreater(float(lifted[head].mean()), float(image[head].mean()) + 20)
        self.assertEqual(record[0]["local"], "shadows")

    def test_the_whole_frame_lift_is_what_it_was_and_changes_the_sky(self):
        image = scene()
        lifted, record = ops.run_low_light(image, "opencv_exposure", 1.0)
        self.assertGreater(float(np.abs(lifted[10:90, 20:600].astype(int) - image[10:90, 20:600].astype(int)).max()), 5)
        self.assertNotIn("local", record[0])

    def test_a_smaller_blend_lifts_less(self):
        image = scene()
        head = (slice(250, 330), slice(280, 350))
        full = ops.run_low_light(image, "opencv_exposure", 1.0, "shadows")[0][head].mean()
        gentle = ops.run_low_light(image, "opencv_exposure", 0.65, "shadows")[0][head].mean()
        self.assertGreater(float(full), float(gentle))
        self.assertGreater(float(gentle), float(image[head].mean()))


@override_settings(**CLASSICAL, ADAPTIVE_ALWAYS_ENHANCE=False, ADAPTIVE_BACKLIT_SUBJECT_LIFT=True)     # the lift is off by default: these tests are about what it does when on
class RouterTests(SimpleTestCase):
    def step(self, style, image=None, level="auto"):
        report = analyzer_with(HEAD).analyze(scene() if image is None else image)
        return AdaptiveEnhancementRouter(EngineConfig()).next_step(report, set(), profile=profile_for(style, level)).step

    def test_ai_enhance_proposes_a_shadow_lift_for_a_back_lit_subject(self):
        step = self.step("ai_max")
        self.assertEqual(step.operation, ops.LOW_LIGHT)
        self.assertTrue(step.optional)
        self.assertEqual([variant["blend"] for variant in step.variants], [1.0, 0.65])
        self.assertTrue(all(variant["local"] == "shadows" for variant in step.variants))
        self.assertIn("back-lit", step.reason)

    def test_the_other_styles_leave_the_back_lit_subject_as_photographed(self):
        for style in ("natural", "dynamic"):
            for level in ("auto", "high"):
                with self.subTest(style=style, level=level):
                    step = self.step(style, level=level)
                    self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)

    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=False)
    def test_the_off_switch_gives_the_earlier_behaviour(self):
        step = self.step("ai_max")
        self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)

    def test_a_photograph_that_is_not_back_lit_is_not_touched(self):
        step = self.step("ai_max", image=portrait())
        self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)

    def test_a_dark_scene_keeps_the_whole_frame_lift(self):
        report = analyzer_with(None).analyze(portrait(brightness=0.12))
        step = AdaptiveEnhancementRouter(EngineConfig()).next_step(report, set(), profile=profile_for("ai_max")).step
        self.assertEqual(step.operation, ops.LOW_LIGHT)
        self.assertTrue(all("local" not in variant for variant in step.variants))

    def test_the_profile_says_so(self):
        self.assertTrue(profile_for("ai_max").describe()["lift_backlit_subjects"])
        self.assertFalse(profile_for("natural").describe()["lift_backlit_subjects"])
        self.assertFalse(profile_for("dynamic", "high").describe()["lift_backlit_subjects"])


@override_settings(**CLASSICAL, ADAPTIVE_ALWAYS_ENHANCE=False, ADAPTIVE_BACKLIT_SUBJECT_LIFT=True)
class EngineTests(SimpleTestCase):
    def run_engine(self, image, style, head=HEAD, **options):
        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(head), config=EngineConfig(), runners={})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            output, _level, parameters = service.enhance(image, requested_level="auto", style=style, **options)
        return output, parameters["adaptive"]

    def test_ai_enhance_lifts_the_subject_and_leaves_the_sky(self):
        image = scene()
        output, adaptive = self.run_engine(image, "ai_max")
        self.assertIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        self.assertGreater(float(output[250:330, 280:350].mean()), float(image[250:330, 280:350].mean()) + 15)
        self.assertLessEqual(int(np.abs(output[10:90, 20:600].astype(int) - image[10:90, 20:600].astype(int)).max()), 2)
        stage = next(item for item in adaptive["stages"] if item["operation"] == ops.LOW_LIGHT)
        self.assertEqual(stage["status"], "accepted")
        self.assertIn("back-lit", stage["reason"])
        self.assertTrue(adaptive["quality_report"]["subject_exposure"]["backlit"])

    def test_natural_leaves_it_alone(self):
        image = scene()
        output, adaptive = self.run_engine(image, "natural")
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        np.testing.assert_array_equal(output, image)

    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=False)
    def test_with_the_switch_off_nothing_is_lifted(self):
        image = scene()
        output, adaptive = self.run_engine(image, "ai_max")
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        np.testing.assert_array_equal(output, image)

    def test_a_well_exposed_portrait_is_left_as_it_was(self):
        image = portrait()
        output, adaptive = self.run_engine(image, "ai_max", head=(150, 100, 180, 220))
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])

    def test_a_heavily_compressed_photograph_is_not_lifted_because_the_lift_would_show_its_blocks(self):
        image = jpeg(scene(**SUBJECT_WITH_STRUCTURE), 40)
        self.assertFalse(measure_compression_artifacts(scene(**SUBJECT_WITH_STRUCTURE))["detected"])        # the same photograph uncompressed is lifted (the first test of this class)
        self.assertTrue(measure_compression_artifacts(image)["detected"])
        output, adaptive = self.run_engine(image, "ai_max")
        self.assertTrue(adaptive["quality_report"]["subject_exposure"]["backlit"])    # it was recognised as back-lit: the lift was refused, not skipped
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        stage = next(item for item in adaptive["stages"] if item["operation"] == ops.LOW_LIGHT)
        self.assertEqual(stage["status"], "rejected")
        self.assertIn("lift_shows_compression_blocks", stage["rejection_reasons"])      # (the gentler variant also fails "clearer"; the gate alone is isolated in CompressionGateTests)
        np.testing.assert_array_equal(output, image)

    def test_an_ordinary_jpeg_is_lifted(self):
        image = jpeg(scene(**SUBJECT_WITH_STRUCTURE), 80)
        self.assertFalse(measure_compression_artifacts(image)["detected"])
        output, adaptive = self.run_engine(image, "ai_max")
        self.assertIn(ops.LOW_LIGHT, adaptive["operations_applied"])

    def test_a_photograph_the_pipeline_enlarged_is_not_lifted_and_the_stage_says_why(self):
        """The same clean-reading photograph is lifted when the engine sees it as uploaded and refused when preprocessing enlarged it first (the blocks of a tiny JPEG are then 32 px wide and unseen)."""
        image = scene(**SUBJECT_WITH_STRUCTURE)
        _output, adaptive = self.run_engine(image, "ai_max")
        self.assertIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        output, adaptive = self.run_engine(image, "ai_max", source_enlarged=True)
        stage = next(item for item in adaptive["stages"] if item["operation"] == ops.LOW_LIGHT)
        self.assertEqual(stage["status"], "rejected")
        self.assertIn("lift_on_enlarged_source", stage["rejection_reasons"])
        self.assertNotIn("lift_shows_compression_blocks", stage["rejection_reasons"])
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        np.testing.assert_array_equal(output, image)

    def test_a_weaker_lift_is_refused_too_when_the_full_strength_lift_shows_the_blocks(self):
        """In the library the gentler blend of two blocky photographs (ids 182 and 200) read under the analyzer's limit and the result was still damaged."""
        image = scene(**BOTH_STRENGTHS_PASS)
        _output, adaptive = self.run_engine(image, "ai_max")                                  # nothing shows blocks: both strengths pass, the engine keeps the better one
        self.assertIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        region = (slice(250, 330), slice(280, 350))
        stronger = ops.run_low_light(image, "opencv_exposure", 1.0, "shadows")[0][region].mean()
        gentler = ops.run_low_light(image, "opencv_exposure", 0.65, "shadows")[0][region].mean()
        middle = (float(stronger) + float(gentler)) / 2

        def reading(candidate):                    # the analyzer reads only the stronger lift (a brighter subject) as heavily compressed
            return {"blockiness": [1.5, 1.2], "detected": bool(candidate[region].mean() > middle)}

        with patch("enhancement.services.measure_compression_artifacts", side_effect=reading):
            output, adaptive = self.run_engine(image, "ai_max")
        stage = next(item for item in adaptive["stages"] if item["operation"] == ops.LOW_LIGHT)
        self.assertEqual(stage["status"], "rejected")
        self.assertEqual(stage["rejection_reasons"], ["lift_shows_compression_blocks"])
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        np.testing.assert_array_equal(output, image)

    @override_settings(ZERO_DCE_ENABLED=True)
    def test_no_fallback_engine_is_tried_once_a_lift_shows_the_blocks(self):
        """Zero-DCE refused for its blocks, the classical lift came next, passed and was still damaged (library ids 182 and 200): no other engine is asked."""
        image = scene(**BOTH_STRENGTHS_PASS)
        engines = []

        def lift(source, engine, variant):
            engines.append(engine)
            candidate = ops.run_low_light(source, "opencv_exposure", variant["blend"], "shadows")[0].copy()
            if engine == "zero_dce":
                candidate[0, 0] = (1, 2, 3)         # marks this engine's output for the reading below
            return candidate, [{"model": engine, "local": "shadows"}]

        def reading(candidate):
            return {"blockiness": [1.5, 1.2], "detected": tuple(int(value) for value in candidate[0, 0]) == (1, 2, 3)}

        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(HEAD), config=EngineConfig(), runners={ops.LOW_LIGHT: lift})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker, patch("enhancement.services.measure_compression_artifacts", side_effect=reading):
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            output, _level, parameters = service.enhance(image, requested_level="auto", style="ai_max")
        self.assertEqual(set(engines), {"zero_dce"})
        self.assertNotIn(ops.LOW_LIGHT, parameters["adaptive"]["operations_applied"])
        np.testing.assert_array_equal(output, image)

    @override_settings(ZERO_DCE_ENABLED=True)
    def test_a_fallback_engine_is_still_tried_when_the_first_one_fails_for_another_reason(self):
        image = scene(**BOTH_STRENGTHS_PASS)
        engines = []

        def lift(source, engine, variant):
            engines.append(engine)
            if engine == "zero_dce":
                return source.copy(), [{"model": engine, "local": "shadows"}]       # no lift at all: refused as "no clearer subject", which is not the blocks
            return ops.run_low_light(source, "opencv_exposure", variant["blend"], "shadows")[0], [{"model": engine, "local": "shadows"}]

        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(HEAD), config=EngineConfig(), runners={ops.LOW_LIGHT: lift})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            _output, _level, parameters = service.enhance(image, requested_level="auto", style="ai_max")
        self.assertEqual(engines[0], "zero_dce")
        self.assertIn("opencv_exposure", engines)
        self.assertIn(ops.LOW_LIGHT, parameters["adaptive"]["operations_applied"])

    @override_settings(ZERO_DCE_ENABLED=True)
    def test_without_blocks_the_first_engine_that_passes_is_enough(self):
        image = scene(**BOTH_STRENGTHS_PASS)
        engines = []

        def lift(source, engine, variant):
            engines.append(engine)
            return ops.run_low_light(source, "opencv_exposure", variant["blend"], "shadows")[0], [{"model": engine, "local": "shadows"}]

        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(HEAD), config=EngineConfig(), runners={ops.LOW_LIGHT: lift})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            _output, _level, parameters = service.enhance(image, requested_level="auto", style="ai_max")
        self.assertEqual(set(engines), {"zero_dce"})
        self.assertIn(ops.LOW_LIGHT, parameters["adaptive"]["operations_applied"])


class CompressionGateTests(SimpleTestCase):
    """The validator's gate for a shadow lift, on its own: a heavily compressed photograph (JPEG quality 40) and the same scene uncompressed. Nothing else is wrong with either."""

    def verdict(self, image, local):
        analyzer = analyzer_with(HEAD)
        report = analyzer.analyze(image)
        candidate, _models = ops.run_low_light(image, "opencv_exposure", 1.0, "shadows")
        validator = FinalImageQualityValidator(analyzer, report)
        return validator.evaluate(image, candidate, ops.LOW_LIGHT, report, **({"local": local} if local else {}))

    def test_a_shadow_lift_that_shows_the_blocks_is_refused(self):
        verdict = self.verdict(jpeg(scene(**SUBJECT_WITH_STRUCTURE), 40), "shadows")
        self.assertFalse(verdict["gates"]["compression_blocks"]["passed"])
        self.assertEqual(verdict["gates"]["compression_blocks"]["status"], "heavy")
        self.assertEqual(verdict["reasons"], ["lift_shows_compression_blocks"])
        self.assertFalse(verdict["accepted"])

    def test_a_shadow_lift_of_an_uncompressed_photograph_passes_it(self):
        verdict = self.verdict(scene(**SUBJECT_WITH_STRUCTURE), "shadows")
        self.assertTrue(verdict["gates"]["compression_blocks"]["passed"])
        self.assertNotIn("lift_shows_compression_blocks", verdict["reasons"])
        self.assertTrue(verdict["accepted"])

    def test_the_gate_reads_the_lifted_photograph_not_the_original(self):
        """In the library the lift of two photographs turned "no compression" into "heavy" (blocks made visible), and the lift of another reads cleaner than its original."""
        image = scene(**SUBJECT_WITH_STRUCTURE)
        analyzer = analyzer_with(HEAD)
        report = analyzer.analyze(image)
        candidate, _models = ops.run_low_light(image, "opencv_exposure", 1.0, "shadows")
        lifted = analyzer.analyze(candidate)
        validator = FinalImageQualityValidator(analyzer, report)
        report.compression, lifted.compression = {"status": "none", "blockiness": [1.1, 1.1]}, {"status": "heavy", "blockiness": [2.0, 1.4]}
        refused = validator.evaluate(image, candidate, ops.LOW_LIGHT, report, candidate_report=lifted, local="shadows")
        self.assertFalse(refused["gates"]["compression_blocks"]["passed"])
        report.compression, lifted.compression = {"status": "heavy", "blockiness": [2.0, 1.4]}, {"status": "none", "blockiness": [1.1, 1.1]}
        allowed = validator.evaluate(image, candidate, ops.LOW_LIGHT, report, candidate_report=lifted, local="shadows")
        self.assertTrue(allowed["gates"]["compression_blocks"]["passed"])

    def test_a_whole_frame_lift_is_not_judged_on_it(self):
        verdict = self.verdict(jpeg(scene(**SUBJECT_WITH_STRUCTURE), 40), None)
        self.assertNotIn("compression_blocks", verdict["gates"])
        self.assertNotIn("lift_shows_compression_blocks", verdict["reasons"])

    # -- a photograph the pipeline enlarged before the engine saw it -----------------------------------------------------------------
    def enlarged_verdict(self, image, local="shadows", source_enlarged=True):
        analyzer = analyzer_with(HEAD)
        report = analyzer.analyze(image)
        candidate, _models = ops.run_low_light(image, "opencv_exposure", 1.0, "shadows")
        validator = FinalImageQualityValidator(analyzer, report, source_enlarged=source_enlarged)
        return validator.evaluate(image, candidate, ops.LOW_LIGHT, report, **({"local": local} if local else {}))

    def test_a_shadow_lift_of_a_photograph_the_pipeline_enlarged_is_refused_even_though_it_reads_clean(self):
        """A 260 px JPEG enlarged 4x reads "no compression" (its blocks are 32 px wide) and its lift came back as a blocky face (library id 182, run through the app on 2026-11-06)."""
        image = scene(**SUBJECT_WITH_STRUCTURE)
        self.assertTrue(self.enlarged_verdict(image, source_enlarged=False)["accepted"])           # the same photograph, not enlarged by the pipeline: lifted
        verdict = self.enlarged_verdict(image)
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["gates"]["compression_blocks"]["status"], "none")                 # the reading did not see anything
        self.assertTrue(verdict["gates"]["compression_blocks"]["source_enlarged"])
        self.assertFalse(verdict["gates"]["compression_blocks"]["passed"])
        self.assertEqual(verdict["reasons"], ["lift_on_enlarged_source"])                          # (and says why, not "shows blocks")

    def test_a_whole_frame_lift_of_an_enlarged_photograph_is_not_judged_on_it(self):
        verdict = self.enlarged_verdict(scene(**SUBJECT_WITH_STRUCTURE), local=None)
        self.assertNotIn("compression_blocks", verdict["gates"])
        self.assertNotIn("lift_on_enlarged_source", verdict["reasons"])

    def test_the_gate_keeps_its_own_reason_for_a_photograph_that_was_not_enlarged(self):
        verdict = self.enlarged_verdict(jpeg(scene(**SUBJECT_WITH_STRUCTURE), 40), source_enlarged=False)
        self.assertEqual(verdict["reasons"], ["lift_shows_compression_blocks"])


class WeakerLiftTests(SimpleTestCase):
    """``_refuse_weaker_shadow_lifts`` on the verdicts alone (the engine-level case is in EngineTests)."""

    @staticmethod
    def candidate(accepted, blocks=None):
        gates = {} if blocks is None else {"compression_blocks": {"passed": blocks}}
        return SimpleNamespace(verdict={"accepted": accepted, "reasons": [], "gates": gates}, release=Mock())

    def test_the_weaker_lift_that_passed_is_refused_with_the_reason_when_the_stronger_one_shows_the_blocks(self):
        stronger, weaker = self.candidate(False, blocks=False), self.candidate(True, blocks=True)
        self.assertTrue(AdaptiveImageEnhancementService._refuse_weaker_shadow_lifts([stronger, weaker]))      # True: a shadow lift shows the blocks
        self.assertFalse(weaker.verdict["accepted"])
        self.assertEqual(weaker.verdict["reasons"], ["lift_shows_compression_blocks"])
        self.assertFalse(weaker.verdict["gates"]["compression_blocks"]["passed"])
        weaker.release.assert_called_once_with()
        stronger.release.assert_not_called()                # refused earlier: its pixels were released then
        self.assertEqual(stronger.verdict["reasons"], [])

    def test_nothing_changes_when_no_shadow_lift_shows_the_blocks(self):
        both = [self.candidate(True, blocks=True), self.candidate(True, blocks=True)]
        self.assertFalse(AdaptiveImageEnhancementService._refuse_weaker_shadow_lifts(both))
        for candidate in both:
            self.assertTrue(candidate.verdict["accepted"])
            self.assertEqual(candidate.verdict["reasons"], [])
            candidate.release.assert_not_called()

    def test_a_whole_frame_lift_has_no_such_gate_and_is_left_alone(self):
        whole_frame = [self.candidate(True), self.candidate(False)]
        self.assertFalse(AdaptiveImageEnhancementService._refuse_weaker_shadow_lifts(whole_frame))
        self.assertTrue(whole_frame[0].verdict["accepted"])
        self.assertEqual(whole_frame[0].verdict["reasons"], [])
        whole_frame[0].release.assert_not_called()

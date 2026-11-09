"""A frame read as defocused while the MAIN face is sharp and prominent is a portrait with a soft background, not a photograph whose blur "cannot be recovered".

The verdict sentence ("Not fully corrected: Defocus blur cannot be recovered reliably."), the outcome (``partially_enhanced``) and the unresolved list leave it out.
Reporting only: the analysis still records ``defocus_blur``, the deblur step's own skip record stays, and no pixel depends on the rule.
Models are replaced by stand-ins; what is tested is the decision and what the result says.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.service import (
    BACKGROUND_DEFOCUS_REASON, ENHANCED, ORIGINAL_PRESERVED, PARTIALLY_ENHANCED, AdaptiveImageEnhancementService, EngineConfig,
    background_defocus_only,
)
from .test_adaptive_engine import ALL_ON, StubDetector, landmarks_for, portrait, runner_lift

DEFOCUSED_FRAME = {"status": "high", "type": "defocus", "retention": 0.8, "direction_ratio": 0.5}
LEFT_UNRECOVERED = "Defocus blur cannot be recovered reliably."


def report(conditions=("defocus_blur",), area=0.12, face_blur="none"):
    main = SimpleNamespace(area_ratio=area, box=(0, 0, 100, 100)) if area is not None else None
    return SimpleNamespace(conditions=list(conditions), main_face=main, face_blur={"status": face_blur})


class RuleTests(SimpleTestCase):
    def test_a_sharp_prominent_face_in_a_defocused_frame_is_background_defocus(self):
        self.assertTrue(background_defocus_only(report()))

    def test_no_face_means_the_frame_blur_is_the_photographs(self):
        self.assertFalse(background_defocus_only(report(area=None)))

    def test_a_soft_or_unmeasured_face_is_not_background_defocus(self):
        for status in ("low", "high", "unknown"):
            with self.subTest(face_blur=status):
                self.assertFalse(background_defocus_only(report(face_blur=status)))

    def test_a_far_away_face_says_nothing_about_the_frame(self):
        self.assertFalse(background_defocus_only(report(area=0.01)))

    def test_the_face_share_limit_is_inclusive_and_a_setting(self):
        self.assertTrue(background_defocus_only(report(area=0.03)))
        self.assertFalse(background_defocus_only(report(area=0.0299)))
        with override_settings(ADAPTIVE_BACKGROUND_DEFOCUS_MIN_FACE_AREA=0.2):
            self.assertFalse(background_defocus_only(report(area=0.12)))

    def test_only_a_defocused_frame_is_concerned(self):
        self.assertFalse(background_defocus_only(report(conditions=("low_light",))))

    @override_settings(ADAPTIVE_BACKGROUND_DEFOCUS_NOT_A_DEFECT=False)
    def test_the_setting_gives_the_earlier_behaviour(self):
        self.assertFalse(background_defocus_only(report()))


class FramedAnalyzer(ImageQualityAnalyzer):
    """The real analysis of the photograph, plus a frame that reads as defocused and a main face of a chosen sharpness
    (what a portrait with a soft background reads as; a synthetic one does not, because a sharp patch lowers the frame's blur reading)."""

    face_blur_status = "none"           # what the ORIGINAL's main face reads as
    result_face_blur_status = None      # what the analyses of what the engine made read as, when they should differ
    extra_conditions = ()               # other defects both the original and the result are read as having
    calls = 0

    def analyze(self, image):
        result = super().analyze(image)
        type(self).calls += 1
        result.blur = dict(DEFOCUSED_FRAME)
        for condition in ("defocus_blur", *self.extra_conditions):
            if condition not in result.conditions:
                result.conditions = result.conditions + [condition]
        if result.faces:
            first = type(self).calls == 1 or self.result_face_blur_status is None
            result.face_blur = {"status": self.face_blur_status if first else self.result_face_blur_status, "detail": 200.0, "judged_on": "main face"}
        return result


@override_settings(**ALL_ON, ADAPTIVE_ALWAYS_ENHANCE=False)
class EngineTests(SimpleTestCase):
    def run_engine(self, image, face_blur="none", box=(150, 100, 180, 220), runners=None, result_face_blur=None, extra=()):
        faces = [(box, landmarks_for(box))] if box else []
        analyzer = type("Analyzer", (FramedAnalyzer,), {"face_blur_status": face_blur, "result_face_blur_status": result_face_blur, "extra_conditions": tuple(extra), "calls": 0})(
            detector=StubDetector(faces))
        service = AdaptiveImageEnhancementService(analyzer=analyzer, config=EngineConfig(), runners=runners if runners is not None else {ops.LOW_LIGHT: runner_lift})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            return service.enhance(image)[2]

    def test_a_portrait_with_a_soft_background_is_not_reported_as_partly_corrected(self):
        adaptive = self.run_engine(portrait(brightness=0.12))["adaptive"]
        self.assertIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        self.assertEqual(adaptive["outcome"], ENHANCED)
        self.assertEqual(adaptive["unresolved_defects"], [])
        self.assertNotIn("Not fully corrected", adaptive["summary"])
        self.assertNotIn(LEFT_UNRECOVERED, adaptive["summary"])
        self.assertEqual(adaptive["conditions_not_counted_as_defects"], [{"condition": "defocus_blur", "reason": BACKGROUND_DEFOCUS_REASON}])
        self.assertIn("defocus_blur", adaptive["quality_report"]["conditions"])        # the analysis still says what it saw

    def test_another_unresolved_defect_is_still_reported_next_to_a_soft_background(self):
        adaptive = self.run_engine(portrait(brightness=0.12), extra=("visible_noise",))["adaptive"]
        self.assertEqual(adaptive["outcome"], PARTIALLY_ENHANCED)
        self.assertEqual(adaptive["unresolved_defects"], ["visible_noise"])
        self.assertIn("Noise remains.", adaptive["summary"])
        self.assertNotIn(LEFT_UNRECOVERED, adaptive["summary"])
        self.assertEqual([item["condition"] for item in adaptive["conditions_not_counted_as_defects"]], ["defocus_blur"])

    def test_the_original_photographs_face_decides_not_the_result(self):
        sharp_then_soft = self.run_engine(portrait(brightness=0.12), face_blur="none", result_face_blur="low")["adaptive"]
        self.assertEqual(sharp_then_soft["unresolved_defects"], [])
        self.assertEqual(sharp_then_soft["outcome"], ENHANCED)
        soft_then_sharp = self.run_engine(portrait(brightness=0.12), face_blur="low", result_face_blur="none")["adaptive"]
        self.assertIn("defocus_blur", soft_then_sharp["unresolved_defects"])
        self.assertEqual(soft_then_sharp["outcome"], PARTIALLY_ENHANCED)

    def test_a_soft_face_still_reports_the_defocus(self):
        for status in ("low", "high"):
            with self.subTest(face_blur=status):
                adaptive = self.run_engine(portrait(brightness=0.12), face_blur=status)["adaptive"]
                self.assertEqual(adaptive["outcome"], PARTIALLY_ENHANCED)
                self.assertIn("defocus_blur", adaptive["unresolved_defects"])
                self.assertIn(LEFT_UNRECOVERED, adaptive["summary"])
                self.assertEqual(adaptive["conditions_not_counted_as_defects"], [])

    def test_a_photograph_without_a_face_still_reports_the_defocus(self):
        adaptive = self.run_engine(portrait(brightness=0.12), box=None)["adaptive"]
        self.assertEqual(adaptive["outcome"], PARTIALLY_ENHANCED)
        self.assertIn(LEFT_UNRECOVERED, adaptive["summary"])

    def test_a_tiny_face_still_reports_the_defocus(self):
        adaptive = self.run_engine(portrait(brightness=0.12), box=(300, 200, 24, 30))["adaptive"]
        self.assertEqual(adaptive["outcome"], PARTIALLY_ENHANCED)
        self.assertIn(LEFT_UNRECOVERED, adaptive["summary"])

    @override_settings(ADAPTIVE_BACKGROUND_DEFOCUS_NOT_A_DEFECT=False)
    def test_the_off_switch_gives_the_earlier_wording_and_outcome(self):
        adaptive = self.run_engine(portrait(brightness=0.12))["adaptive"]
        self.assertEqual(adaptive["outcome"], PARTIALLY_ENHANCED)
        self.assertIn("defocus_blur", adaptive["unresolved_defects"])
        self.assertIn(LEFT_UNRECOVERED, adaptive["summary"])
        self.assertEqual(adaptive["conditions_not_counted_as_defects"], [])

    def test_when_nothing_was_changed_a_soft_background_is_not_called_a_defect_either(self):
        adaptive = self.run_engine(portrait(), runners={})["adaptive"]
        self.assertEqual(adaptive["outcome"], ORIGINAL_PRESERVED)
        self.assertEqual(adaptive["summary"], "The photograph did not need enhancement, so it was left as it is.")
        # the deblur step's own record stays: what the engine considered and why it did nothing
        self.assertTrue(any(item["operation"] == ops.DEBLUR and "defocus" in item["reason"] for item in adaptive["operations_skipped"]))

    def test_when_nothing_was_changed_a_soft_face_is_still_told_so(self):
        adaptive = self.run_engine(portrait(), face_blur="low", runners={})["adaptive"]
        self.assertEqual(adaptive["outcome"], ORIGINAL_PRESERVED)
        self.assertIn("defocus blur, which cannot be recovered reliably", adaptive["summary"])

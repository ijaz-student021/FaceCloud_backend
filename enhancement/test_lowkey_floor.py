"""A low-key photograph (a lit subject on a BLACK background) is not brightened by the low-light step (auto_edit.is_black_backdrop_low_key, ADAPTIVE_LOWKEY_SKIP_LIFT).

Measured on the owner's portrait: the lift took the dark part from L 8.7 to 23.4 (a milky grey) and the owner pulled it back by hand. A night photograph of a person (dark part 15 showing a wall and trees,
subject at 97) is under-exposed, not low-key, and the lift is exactly what is wanted there. Synthetic photographs here; the real ones are in docs/evaluation/2026-11-04-lowkey-floor.md.
"""
from unittest.mock import patch

import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .auto_edit import is_black_backdrop_low_key, recommend_adaptive_adjustments
from .services import analyze_image_quality
from .test_adaptive_polish import _no_identity
from .test_auto_edit import subject_on


class ClassificationTests(SimpleTestCase):
    def test_a_lit_subject_on_a_black_background_is_low_key(self):
        self.assertTrue(is_black_backdrop_low_key(subject_on(8)))

    def test_a_dark_part_that_is_not_black_is_not(self):
        for background in (20, 30):          # the dark part measures about L 16 and L 29; the limit is 12
            self.assertFalse(is_black_backdrop_low_key(subject_on(background)), background)

    def test_a_dim_subject_is_under_exposed_not_low_key(self):
        self.assertFalse(is_black_backdrop_low_key(subject_on(8, subject=(40, 70, 100))))

    def test_dark_everywhere_and_bright_photographs_are_not(self):
        self.assertFalse(is_black_backdrop_low_key(np.full((240, 320, 3), 8, np.uint8)))
        self.assertFalse(is_black_backdrop_low_key(np.full((240, 320, 3), 200, np.uint8)))

    def test_the_setting_is_on_by_default(self):
        self.assertIs(settings.ADAPTIVE_LOWKEY_SKIP_LIFT, True)


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class EngineTests(SimpleTestCase):
    """Through the engine: does the low-light model run?"""

    def run_engine(self, image, style="natural", level="auto", **settings_):
        calls = []

        def lift(source, engine, variant):
            calls.append(engine)
            return np.clip(source.astype(np.float32) * 2.0 + 10.0, 0, 255).astype(np.uint8), [{"model": engine}]

        service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(None), config=EngineConfig(), runners={ops.LOW_LIGHT: lift})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity), override_settings(**settings_):
            _out, _level, parameters = service.enhance(image, requested_level=level, style=style)
        return calls, parameters["adaptive"]

    def test_the_premise_a_dark_photograph_is_lifted_when_the_setting_is_off(self):
        calls, _adaptive = self.run_engine(subject_on(8), ADAPTIVE_LOWKEY_SKIP_LIFT=False)
        self.assertTrue(calls, "the stand-in photograph must reach the low-light step for the other tests to mean anything")

    def test_a_low_key_photograph_is_not_lifted_and_the_result_says_why(self):
        calls, adaptive = self.run_engine(subject_on(8), ADAPTIVE_LOWKEY_SKIP_LIFT=True)
        self.assertEqual(calls, [])
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        self.assertTrue([item for item in adaptive["operations_skipped"] if item["operation"] == ops.LOW_LIGHT and "left as photographed" in item["reason"]], adaptive["operations_skipped"])

    def test_ai_enhance_and_the_high_strength_still_lift_as_asked(self):
        for style, level in (("ai_max", "auto"), ("natural", "high")):
            calls, _adaptive = self.run_engine(subject_on(8), style=style, level=level, ADAPTIVE_LOWKEY_SKIP_LIFT=True)
            self.assertTrue(calls, (style, level))

    def test_a_night_photograph_whose_dark_part_is_not_black_is_lifted_as_before(self):
        night = subject_on(15, subject=(60, 90, 120))
        on, _ = self.run_engine(night, ADAPTIVE_LOWKEY_SKIP_LIFT=True)
        off, _ = self.run_engine(night, ADAPTIVE_LOWKEY_SKIP_LIFT=False)
        self.assertEqual(on, off)

    def test_a_dark_photograph_with_nothing_lit_is_lifted_as_before(self):
        dark = np.full((320, 240, 3), 20, np.uint8)
        on, _ = self.run_engine(dark, ADAPTIVE_LOWKEY_SKIP_LIFT=True)
        off, _ = self.run_engine(dark, ADAPTIVE_LOWKEY_SKIP_LIFT=False)
        self.assertEqual(on, off)


class EditorAutoUsesTheSameConditionTests(SimpleTestCase):
    def test_a_dark_part_that_is_not_black_in_the_upload_is_left_alone_by_auto(self):
        upload, enhanced = subject_on(25), subject_on(35)
        result = recommend_adaptive_adjustments(enhanced, analyze_image_quality(enhanced), baseline=upload)
        self.assertEqual(result["controls"]["brightness"], 0.0)
        self.assertTrue(any("not black" in line for line in result["rationale"]))

    def test_a_black_upload_that_was_lifted_is_still_put_back(self):
        upload, enhanced = subject_on(8), subject_on(24)
        result = recommend_adaptive_adjustments(enhanced, analyze_image_quality(enhanced), baseline=upload)
        self.assertLess(result["controls"]["brightness"], -30)

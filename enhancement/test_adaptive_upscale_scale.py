"""The size factor of the enlargement the engine proposes for a small photograph (ADAPTIVE_UPSCALE_SCALE): 2 by default, exactly as before, and 4 when asked.

Measured with real models (docs/evaluation/2026-11-06-enlargement-after-lift.md, section 6): six small photographs were all enlarged 4x with every gate passing. The models are stand-ins here: what is
tested is the choice of the factor, the fall back to 2 under the output limit, and that a default run is the run it always was.
"""
from unittest.mock import patch

import cv2
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .restoration import describe_stages
from .test_adaptive_enlargement_geometry import BiasedDetector, clear_enlargement
from .test_adaptive_polish import _no_identity

DONE = {ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR, ops.FACE}
FACE = (75, 50, 90, 110)                 # the face of the 320x240 photograph below


def proposed(config, size=(320, 240)):
    report = base.analyzer_with(None).analyze(cv2.resize(base.portrait(), size))
    return AdaptiveEnhancementRouter(config).next_step(report, DONE)


class SettingTests(SimpleTestCase):
    def test_the_default_is_two(self):
        self.assertEqual(settings.ADAPTIVE_UPSCALE_SCALE, 2)
        self.assertEqual(EngineConfig().upscale_scale, 2)

    def test_the_engine_reads_the_setting(self):
        self.assertEqual(EngineConfig.from_settings().upscale_scale, 2)
        with override_settings(ADAPTIVE_UPSCALE_SCALE=4):
            self.assertEqual(EngineConfig.from_settings().upscale_scale, 4)


class RouterScaleTests(SimpleTestCase):
    def test_two_by_default(self):
        self.assertEqual(proposed(EngineConfig()).step.variants, [{"name": "x2", "scale": 2}])

    def test_four_when_asked(self):
        self.assertEqual(proposed(EngineConfig(upscale_scale=4)).step.variants, [{"name": "x4", "scale": 4}])

    def test_four_falls_back_to_two_when_it_would_pass_the_output_limit(self):
        """A 320 px photograph: 2x is 640, 4x is 1280."""
        self.assertEqual(proposed(EngineConfig(upscale_scale=4, max_output_edge=700)).step.variants, [{"name": "x2", "scale": 2}])

    def test_there_is_no_step_when_even_two_would_pass_the_limit(self):
        decision = proposed(EngineConfig(upscale_scale=4, max_output_edge=400))
        self.assertIsNone(decision.step)
        self.assertTrue(any("size limit" in item["reason"] for item in decision.skipped))

    def test_any_other_value_means_two(self):
        for value in (0, 1, 3, 8, -4):
            with self.subTest(value=value):
                self.assertEqual(proposed(EngineConfig(upscale_scale=value)).step.variants[0]["scale"], 2)

    def test_the_output_limit_is_inclusive(self):
        self.assertEqual(proposed(EngineConfig(upscale_scale=4, max_output_edge=1280)).step.variants[0]["scale"], 4)
        self.assertEqual(proposed(EngineConfig(upscale_scale=4, max_output_edge=1279)).step.variants[0]["scale"], 2)


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class EngineTests(SimpleTestCase):
    def run_engine(self, scale):
        source = cv2.resize(base.portrait(), (320, 240))
        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=BiasedDetector(FACE, 0.0, 320)), config=EngineConfig(upscale_scale=scale),
                                                  runners={ops.UPSCALE: clear_enlargement})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
            output, _level, parameters = service.enhance(source)
        stage = next(s for s in parameters["adaptive"]["stages"] if s["operation"] == ops.UPSCALE)
        return source, output, stage, parameters

    def test_the_result_is_four_times_the_photograph(self):
        source, output, stage, parameters = self.run_engine(4)
        self.assertEqual(stage["status"], "accepted", stage.get("rejection_reasons"))
        self.assertEqual(stage["variant"], "x4")
        self.assertEqual(output.shape[:2], (source.shape[0] * 4, source.shape[1] * 4))
        self.assertEqual(parameters["resolution_contract"]["actual_output_dimensions"], [1280, 960])

    def test_the_default_run_is_twice_the_photograph(self):
        source, output, stage, _ = self.run_engine(2)
        self.assertEqual(stage["status"], "accepted", stage.get("rejection_reasons"))
        self.assertEqual(stage["variant"], "x2")
        self.assertEqual(output.shape[:2], (source.shape[0] * 2, source.shape[1] * 2))

    def test_the_result_says_which_factor_it_applied(self):
        _source, _output, _stage, parameters = self.run_engine(4)
        upscale = next(item for item in describe_stages(parameters)["applied_stages"] if item["stage"] == "upscale")
        self.assertEqual(upscale["scale"], 4)
        self.assertEqual(upscale["output_size"], [1280, 960])

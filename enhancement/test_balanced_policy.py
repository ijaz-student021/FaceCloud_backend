"""``balanced_v1``: the lightweight pipeline plus Zero-DCE (CPU, OpenCV) when a lift is requested; and the
conditional pre-filter that runs before a requested lift of a dark photo.

Zero-DCE itself is replaced by a stand-in here (these tests are about *when* it runs, not what it does).
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import policy
from .services import _chroma_prefilter, enhance_bgr
from .test_pipeline_regressions import NO_MODELS

LEARNED_ADAPTERS = (
    "enhancement.services.restore_faces", "enhancement.services.deblur_with_nafnet",
    "enhancement.services.upscale_with_realesrgan", "enhancement.services.restore_with_purevision",
    "enhancement.services.upscale_with_purescale", "enhancement.services.colorize_with_siggraph17",
)


def dark_photo(factor=0.25, size=(240, 320), seed=3):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size[0], 0:size[1]].astype(np.float32)
    base = 120 + 50 * np.sin(xx / size[1] * 7.0) + 30 * np.cos(yy / size[0] * 5.0)
    image = np.stack([base * 0.8, base, base * 1.1], axis=2)
    dark = np.clip(image * factor, 0, 255)
    return np.clip(dark + rng.normal(0, 1, dark.shape) * (1.5 + 0.35 * np.sqrt(dark)), 0, 255).astype(np.uint8)


def fake_zero_dce(image):
    return np.clip(image.astype(np.float32) * 2.2 + 8, 0, 255).astype(np.uint8), {"provider": "stand_in", "curve_iterations": 8}


class PolicyTableTests(SimpleTestCase):
    def test_each_policy_allows_exactly_its_stages(self):
        self.assertEqual(policy.ALLOWED_STAGES[policy.LIGHTWEIGHT], frozenset())
        self.assertEqual(policy.ALLOWED_STAGES[policy.BALANCED], frozenset({"ZERO_DCE_ENABLED"}))
        self.assertEqual(policy.ALLOWED_STAGES[policy.LEARNED], frozenset(policy.LEARNED_STAGES))

    def test_a_stage_needs_both_the_policy_and_its_own_setting(self):
        for name, expected in ((policy.LIGHTWEIGHT, False), (policy.BALANCED, True), (policy.LEARNED, True)):
            with self.subTest(name), override_settings(ENHANCEMENT_RENDER_POLICY=name, ZERO_DCE_ENABLED=True):
                self.assertIs(policy.learned_stage_enabled("ZERO_DCE_ENABLED"), expected)
            with self.subTest(name + " setting off"), override_settings(ENHANCEMENT_RENDER_POLICY=name, ZERO_DCE_ENABLED=False):
                self.assertFalse(policy.learned_stage_enabled("ZERO_DCE_ENABLED"))
        with override_settings(ENHANCEMENT_RENDER_POLICY=policy.BALANCED, GFPGAN_ENABLED=True):
            self.assertFalse(policy.learned_stage_enabled("GFPGAN_ENABLED"))  # no PyTorch model under balanced

    def test_the_description_names_the_allowed_stage_and_no_heavy_restoration(self):
        with override_settings(ENHANCEMENT_RENDER_POLICY=policy.BALANCED, GFPGAN_ENABLED=True, ZERO_DCE_ENABLED=True):
            described = policy.describe()
        self.assertEqual((described["name"], described["allowed_stages"]), ("balanced_v1", ["zero_dce"]))
        self.assertFalse(described["heavy_restoration_available"])
        self.assertIn("gfpgan_face_restoration", described["configured_on_but_disabled_by_policy"])

    def test_an_unknown_policy_is_refused(self):
        with override_settings(ENHANCEMENT_RENDER_POLICY="fast"), self.assertRaises(ValueError):
            policy.active_policy()


@override_settings(**{**NO_MODELS, "ZERO_DCE_ENABLED": True, "ENHANCEMENT_PREFILTER_MODE": "off"})
class WhenZeroDceRunsTests(SimpleTestCase):
    def run_with(self, policy_name, level):
        with override_settings(ENHANCEMENT_RENDER_POLICY=policy_name), \
                patch("enhancement.services.enhance_with_zero_dce", side_effect=fake_zero_dce) as zero_dce:
            output, applied, parameters = enhance_bgr(dark_photo(), level)
        return zero_dce, output, applied, parameters

    def test_the_default_policy_never_calls_it(self):
        for level in ("auto", "low", "medium", "high"):
            with self.subTest(level):
                zero_dce, *_ = self.run_with(policy.LIGHTWEIGHT, level)
                zero_dce.assert_not_called()

    def test_balanced_automatic_mode_still_does_not_lift_a_dark_photo(self):
        source = dark_photo()
        zero_dce, output, _applied, parameters = self.run_with(policy.BALANCED, "auto")
        zero_dce.assert_not_called()
        self.assertEqual(parameters["algorithm"], "opencv_adaptive_restoration_router_v2")
        self.assertLessEqual(abs(float(output.mean()) - float(source.mean())), 1.0)
        self.assertIn("withheld", parameters["zero_dce"]["fallback_reason"])
        self.assertEqual(parameters["plan"]["routing"][0]["decision"], "lift_withheld")

    def test_balanced_with_a_chosen_level_lifts_through_zero_dce(self):
        source = dark_photo()
        for level in ("medium", "high"):
            with self.subTest(level):
                zero_dce, output, applied, parameters = self.run_with(policy.BALANCED, level)
                zero_dce.assert_called_once()
                self.assertEqual(applied, level)
                self.assertTrue(parameters["zero_dce"]["applied"])
                self.assertIn("zero_dce_hybrid_v1", parameters["algorithm"])
                self.assertGreater(float(output.mean()), float(source.mean()) + 5)
                self.assertEqual(output.shape, source.shape)

    def test_balanced_never_runs_a_pytorch_model(self):
        with override_settings(
            ENHANCEMENT_RENDER_POLICY=policy.BALANCED, GFPGAN_ENABLED=True, HD_RESTORATION_ENABLED=True,
            NAFNET_DEBLUR_ENABLED=True, PURESCALE_ENABLED=True, PUREVISION_ENABLED=True, COLORIZATION_ENABLED=True,
        ), patch("enhancement.services.enhance_with_zero_dce", side_effect=fake_zero_dce):
            patches = [patch(name, side_effect=AssertionError(f"{name} must not run under balanced_v1")) for name in LEARNED_ADAPTERS]
            for each in patches:
                each.start()
            try:
                _output, _applied, parameters = enhance_bgr(dark_photo(), "high")
            finally:
                for each in patches:
                    each.stop()
        self.assertIn("zero_dce", parameters["algorithm"])
        self.assertNotIn("gfpgan", parameters["algorithm"])

    def test_a_missing_zero_dce_model_falls_back_to_the_opencv_lift_and_says_why(self):
        from .zero_dce import ZeroDCEUnavailable

        with override_settings(ENHANCEMENT_RENDER_POLICY=policy.BALANCED), \
                patch("enhancement.services.enhance_with_zero_dce", side_effect=ZeroDCEUnavailable("model missing")):
            output, _applied, parameters = enhance_bgr(dark_photo(), "high")
        self.assertFalse(parameters["zero_dce"]["applied"])
        self.assertEqual(parameters["zero_dce"]["fallback_reason"], "model missing")
        self.assertEqual(parameters["algorithm"], "opencv_adaptive_restoration_router_v2")
        self.assertGreater(float(output.mean()), float(dark_photo().mean()))


@override_settings(**{**NO_MODELS, "ENHANCEMENT_RENDER_POLICY": policy.LIGHTWEIGHT})
class PrefilterTests(SimpleTestCase):
    def run_with(self, image, level, mode="chroma"):
        with override_settings(ENHANCEMENT_PREFILTER_MODE=mode):
            return enhance_bgr(image, level)

    def test_it_runs_before_a_requested_lift_of_a_dark_photo_and_says_so(self):
        _out, _level, parameters = self.run_with(dark_photo(), "high")
        record = parameters["prefilter"]
        self.assertTrue(record["applied"])
        self.assertEqual(record["mode"], "chroma")
        self.assertGreater(record["mean_change"], 0)

    def test_automatic_mode_withholds_the_lift_and_therefore_does_not_filter(self):
        source = dark_photo()
        output, _level, parameters = self.run_with(source, "auto")
        self.assertEqual(parameters["prefilter"], {})  # exposure was skipped before the filter was considered
        self.assertLessEqual(abs(float(output.mean()) - float(source.mean())), 1.0)

    def test_it_does_not_run_on_a_photo_that_is_not_dark(self):
        bright = np.clip(dark_photo(1.0) .astype(np.float32) * 0.5 + 60, 0, 255).astype(np.uint8)
        _out, _level, parameters = self.run_with(bright, "high")
        self.assertFalse(parameters["prefilter"].get("applied"))
        self.assertIn("not dark enough", parameters["prefilter"]["reason"])

    def test_it_can_be_switched_off(self):
        _out, _level, parameters = self.run_with(dark_photo(), "high", mode="off")
        self.assertFalse(parameters["prefilter"]["applied"])
        self.assertEqual(parameters["prefilter"]["reason"], "switched off")

    def test_nlm_mode_is_used_for_a_small_photo_and_falls_back_to_chroma_for_a_large_one(self):
        _out, _level, small = self.run_with(dark_photo(), "high", mode="nlm")
        self.assertIn("non-local-means", small["prefilter"]["method"])
        with patch("enhancement.services._PREFILTER_NLM_MAX_PIXELS", 100):
            _out, _level, large = self.run_with(dark_photo(), "high", mode="nlm")
        self.assertIn("chroma-only", large["prefilter"]["method"])
        self.assertIn("too large", large["prefilter"]["reason"])

    def test_the_chroma_filter_leaves_luminance_untouched_and_reduces_colour_noise(self):
        noisy = dark_photo(0.3, size=(300, 400))
        filtered = _chroma_prefilter(noisy)
        self.assertEqual((filtered.shape, filtered.dtype), (noisy.shape, noisy.dtype))
        before, after = cv2.cvtColor(noisy, cv2.COLOR_BGR2YCrCb), cv2.cvtColor(filtered, cv2.COLOR_BGR2YCrCb)
        # luminance is only touched by the BGR<->YCrCb round trip (rounding), not by the filter
        self.assertLessEqual(float(np.abs(before[..., 0].astype(int) - after[..., 0].astype(int)).mean()), 1.0)
        residual = lambda plane: float(np.std(plane.astype(np.float32) - cv2.GaussianBlur(plane.astype(np.float32), (0, 0), 3)))
        self.assertLess(residual(after[..., 1]) + residual(after[..., 2]), 0.6 * (residual(before[..., 1]) + residual(before[..., 2])))

    def test_a_filtered_lift_is_not_noisier_than_an_unfiltered_one(self):
        source = dark_photo(0.22, size=(300, 400), seed=11)
        noise = lambda image: float(np.std(cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)[..., 1:].astype(np.float32)
                                           - cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)[..., 1:].astype(np.float32), (0, 0), 3)))
        plain, *_ = self.run_with(source, "high", mode="off")
        filtered, *_ = self.run_with(source, "high", mode="chroma")
        self.assertLess(noise(filtered), noise(plain))

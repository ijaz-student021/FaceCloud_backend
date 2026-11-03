"""Style and strength change what the adaptive engine tries (profile.py); colourization of black-and-white photographs; the DDColor adapter's safety.

The models are replaced by stand-ins here (CI has no weights); the real network was run on the owner's portrait and on grey-converted photographs, see
docs/evaluation/2026-11-03-colourization.md.
"""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import colorize
from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator, Limits
from .test_adaptive_polish import _no_identity

COLOUR_ON = dict(base.ALL_ON, DDCOLOR_ENABLED=True)
NEEDS_TORCH = unittest.skipUnless(importlib.util.find_spec("torch"), "PyTorch is not installed (CI); these run the network code with a stand-in")
EVERYTHING_BUT_COLOUR = set(ops.ORDER) - {ops.COLORIZE}


def grey(image):
    return cv2.cvtColor(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


def tinted(image, amount):
    """The photograph with its chroma shifted by ``amount`` 8-bit LAB units: a toned print, not black and white."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.int16)
    lab[:, :, 1] += amount
    lab[:, :, 2] += amount
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def warm_colourization(image, engine, variant):
    """A stand-in for DDColor: the image's own lightness with a mild warm chroma (what a good, damped prediction looks like)."""
    lab = cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
    lab[:, :, 1], lab[:, :, 2] = 6.0, 12.0
    return np.clip(np.rint(cv2.cvtColor(lab, cv2.COLOR_Lab2BGR) * 255.0), 0, 255).astype(np.uint8), [{"model": engine}]


class ProfileMappingTests(SimpleTestCase):
    def test_natural_and_auto_is_what_the_engine_always_did(self):
        profile = profile_for("natural", "auto")
        self.assertEqual((profile.colourize, profile.polish_style, profile.polish_scale, profile.polish_dark_scenes, profile.lift_dark_scenes,
                          profile.treat_low_noise, profile.upscale_below_edge, profile.variant_blends),
                         (False, "natural", 1.0, False, False, False, 0, (1.0, 0.65)))
        self.assertEqual(profile_for("natural", "medium").describe() | {"level": "auto"}, profile.describe())

    def test_dynamic_adds_punch_dark_scene_polish_and_colour(self):
        profile = profile_for("dynamic")
        self.assertEqual((profile.colourize, profile.polish_style, profile.polish_dark_scenes, profile.polish_scale), (True, "dynamic", True, 1.0))

    def test_ai_max_does_everything_dynamic_does_and_more(self):
        profile = profile_for("ai_max")
        self.assertEqual((profile.colourize, profile.polish_style, profile.polish_scale, profile.treat_low_noise, profile.upscale_below_edge, profile.lift_dark_scenes),
                         (True, "dynamic", 1.25, True, 1280, True))

    def test_the_strength_scales_the_style(self):
        low, high = profile_for("dynamic", "low"), profile_for("dynamic", "high")
        self.assertEqual((low.polish_scale, low.variant_blends, low.lift_dark_scenes), (0.6, (0.65, 0.4), False))
        self.assertEqual((high.polish_scale, high.lift_dark_scenes, high.treat_low_noise, high.upscale_below_edge), (1.4, True, True, 960))
        self.assertEqual(profile_for("ai_max", "high").upscale_below_edge, 1280)      # never lowered by the strength

    def test_unknown_values_fall_back_to_natural_and_enums_are_accepted(self):
        from types import SimpleNamespace

        self.assertEqual(profile_for("loud").style, "natural")
        self.assertEqual(profile_for(SimpleNamespace(value="ai_max"), SimpleNamespace(value="high")).describe()["style"], "ai_max")
        self.assertEqual(profile_for(None, None).level, "auto")


@override_settings(**COLOUR_ON)
class RouterProfileTests(SimpleTestCase):
    def router(self):
        return AdaptiveEnhancementRouter(EngineConfig())

    def report(self, image=None, **changes):
        report = base.analyzer_with(None).analyze(image if image is not None else base.portrait())
        for name, value in changes.items():
            setattr(report, name, value)
        return report

    def step(self, report, profile, done=EVERYTHING_BUT_COLOUR, polish=None):
        return self.router().next_step(report, set(done), polish, profile).step

    # -- black and white ---------------------------------------------------------------------------------------------
    def test_the_analyzer_knows_a_black_and_white_photograph_and_a_toned_one_is_not(self):
        self.assertTrue(self.report(grey(base.portrait())).colour["greyscale"])
        self.assertFalse(self.report(base.portrait()).colour["greyscale"])
        self.assertFalse(self.report(tinted(grey(base.portrait()), 10)).colour["greyscale"], "a tinted print keeps its tint")

    def test_dynamic_and_ai_max_colourize_a_black_and_white_photograph_natural_does_not(self):
        report = self.report(grey(base.portrait()))
        self.assertIsNone(self.step(report, profile_for("natural")))
        for style in ("dynamic", "ai_max"):
            step = self.step(report, profile_for(style))
            self.assertEqual((step.operation, step.engines), (ops.COLORIZE, ["ddcolor"]), style)

    def test_a_colour_photograph_is_never_colourized(self):
        self.assertIsNone(self.step(self.report(base.portrait()), profile_for("ai_max", "high")))

    def test_colourization_is_considered_last_and_only_once(self):
        report = self.report(grey(base.portrait()))
        self.assertEqual(ops.ORDER[-1], ops.COLORIZE)
        self.assertIsNone(self.step(report, profile_for("dynamic"), done=set(ops.ORDER)))

    @override_settings(DDCOLOR_ENABLED=False)
    def test_with_the_model_off_the_reason_is_recorded(self):
        decision = self.router().next_step(self.report(grey(base.portrait())), set(EVERYTHING_BUT_COLOUR), None, profile_for("dynamic"))
        self.assertIsNone(decision.step)
        self.assertIn({"operation": ops.COLORIZE, "reason": "No colourization model is switched on."}, decision.skipped)

    # -- dark scenes --------------------------------------------------------------------------------------------------
    def dark_scene(self):
        return self.report(exposure={"status": "dark_scene", "judged_on": "subject", "scene_median": 40.0})

    def test_a_dark_scene_is_left_alone_unless_the_strength_is_strong_or_the_style_is_ai_max(self):
        self.assertEqual(self.router().next_step(self.dark_scene(), set(), None, profile_for("ai_max")).step.operation, ops.LOW_LIGHT)
        for profile in (profile_for("natural"), profile_for("dynamic")):
            decision = self.router().next_step(self.dark_scene(), set(), None, profile)
            self.assertNotEqual(getattr(decision.step, "operation", None), ops.LOW_LIGHT, profile.style)
            self.assertTrue(any("left as photographed" in item["reason"] for item in decision.skipped), profile.style)
        step = self.router().next_step(self.dark_scene(), set(), None, profile_for("natural", "high")).step
        self.assertEqual(step.operation, ops.LOW_LIGHT)
        self.assertIn("lifts a dark scene", step.reason)

    def test_polish_reaches_a_dark_scene_only_with_dynamic_or_ai_max(self):
        report = self.dark_scene()
        self.assertIsNone(self.step(report, profile_for("natural"), done={ops.LOW_LIGHT}, polish={"style": "natural"}))
        step = self.step(report, profile_for("dynamic"), done={ops.LOW_LIGHT}, polish={"style": "dynamic"})
        self.assertEqual(step.operation, ops.POLISH)

    # -- polish strength ----------------------------------------------------------------------------------------------
    def test_the_strength_scales_the_polish_and_it_is_capped(self):
        def shares(profile, style="dynamic"):
            variant = self.step(self.report(), profile, polish={"style": style}, done={ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR, ops.FACE, ops.UPSCALE}).variants[0]
            return variant["clahe"], variant["amount"]

        self.assertEqual(shares(profile_for("dynamic")), (0.55, 0.5))
        self.assertEqual(shares(profile_for("dynamic", "low")), (0.33, 0.3))
        self.assertEqual(shares(profile_for("ai_max", "high")), (0.8, 0.7))                  # 0.55 x 1.75 and 0.5 x 1.75, both capped
        self.assertEqual(shares(profile_for("natural", "high"), "natural"), (0.35, 0.35))

    # -- noise, resolution, blends ----------------------------------------------------------------------------------
    def test_low_noise_is_treated_only_by_ai_max_or_a_strong_level(self):
        report = self.report(noise={"status": "low", "estimate": 2.0, "level": 0.5})
        done = {ops.LOW_LIGHT}
        for profile, treated in ((profile_for("natural"), False), (profile_for("dynamic"), False), (profile_for("ai_max"), True), (profile_for("natural", "high"), True)):
            step = self.router().next_step(report, set(done), None, profile).step
            self.assertEqual(getattr(step, "operation", None) == ops.DENOISE, treated, profile.describe())

    def test_a_modest_photograph_is_enlarged_only_when_the_style_or_strength_asks_for_it(self):
        photo = np.full((700, 1000, 3), 120, np.uint8)
        cv2.randu(photo, 90, 150)
        report = self.report(photo)
        self.assertEqual(report.resolution["status"], "normal")
        done = set(ops.ORDER) - {ops.UPSCALE}
        self.assertIsNone(self.step(report, profile_for("natural"), done=done))
        step = self.step(report, profile_for("ai_max"), done=done)
        self.assertEqual((step.operation, step.variants), (ops.UPSCALE, [{"name": "x2", "scale": 2}]))
        self.assertIsNone(self.step(report, profile_for("natural", "high"), done=done), "1000 px is above the strong level's 960 px line")

    def test_a_light_strength_tries_gentler_blends(self):
        report = self.report(base.portrait(brightness=0.12))
        self.assertEqual([v["blend"] for v in self.router().next_step(report, set(), None, profile_for("natural", "low")).step.variants], [0.65, 0.4])
        self.assertEqual([v["blend"] for v in self.router().next_step(report, set()).step.variants], [1.0, 0.65])      # no profile: as before


@override_settings(**COLOUR_ON)
class ColourizationEngineTests(SimpleTestCase):
    def run_engine(self, image, style, runner=warm_colourization, level="auto"):
        service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(None), config=EngineConfig(), runners={ops.COLORIZE: runner})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
            return service.enhance(image, requested_level=level, style=style)

    def test_a_black_and_white_photograph_is_colourized_for_dynamic_and_ai_max_with_the_lightness_kept(self):
        source = grey(base.portrait())
        for style in ("dynamic", "ai_max"):
            out, _level, parameters = self.run_engine(source, style)
            adaptive = parameters["adaptive"]
            self.assertEqual(adaptive["operations_applied"], [ops.COLORIZE], style)
            self.assertIn("colourized the black-and-white photograph", adaptive["summary"])
            self.assertEqual(adaptive["profile"]["style"], style)
            self.assertGreater(float(np.abs(out[:, :, 0].astype(int) - out[:, :, 2].astype(int)).mean()), 3.0, "it is no longer grey")
            self.assertGreater(cv2.PSNR(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY), cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)), 38.0)

    def test_natural_keeps_a_black_and_white_photograph_black_and_white(self):
        source = grey(base.portrait())
        out, _level, parameters = self.run_engine(source, "natural")
        np.testing.assert_array_equal(out, source)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [])

    def test_a_colour_photograph_is_never_touched_by_colourization(self):
        source = base.portrait()
        calls = []
        self.run_engine(source, "ai_max", runner=lambda image, engine, variant: calls.append(1) or warm_colourization(image, engine, variant))
        self.assertEqual(calls, [])

    def test_a_missing_model_leaves_the_photograph_black_and_white_and_says_why(self):
        def missing(image, engine, variant):
            raise colorize.DDColorUnavailable("The DDColor checkpoint is unavailable.")

        source = grey(base.portrait())
        out, _level, parameters = self.run_engine(source, "dynamic", runner=missing)
        np.testing.assert_array_equal(out, source)
        stage = next(s for s in parameters["adaptive"]["stages"] if s["operation"] == ops.COLORIZE)
        self.assertEqual(stage["status"], "unavailable")
        self.assertEqual(stage["skipped_engines"][0]["reason"], "The DDColor checkpoint is unavailable.")


class ColourizationValidatorTests(SimpleTestCase):
    def verdict(self, parent, candidate):
        validator = FinalImageQualityValidator(base.analyzer_with(None), base.analyzer_with(None).analyze(parent))
        return validator.evaluate(parent, candidate, ops.COLORIZE, base.analyzer_with(None).analyze(parent))

    def setUp(self):
        self.parent = grey(base.portrait())

    def test_a_plausible_colourization_is_accepted(self):
        verdict = self.verdict(self.parent, warm_colourization(self.parent, "ddcolor", {})[0])
        self.assertTrue(verdict["accepted"], verdict["reasons"])
        self.assertGreater(verdict["improvement"], 0.0)

    def test_an_unchanged_grey_image_is_refused_as_no_colour_added(self):
        self.assertIn("no_colour_added", self.verdict(self.parent, self.parent.copy())["reasons"])

    def test_a_changed_lightness_is_refused(self):
        brighter = np.clip(warm_colourization(self.parent, "ddcolor", {})[0].astype(np.int16) + 40, 0, 255).astype(np.uint8)
        self.assertIn("lightness_changed", self.verdict(self.parent, brighter)["reasons"])

    def test_a_runaway_saturation_is_refused(self):
        lab = cv2.cvtColor(self.parent.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
        lab[:, :, 1], lab[:, :, 2] = 70.0, 60.0
        vivid = np.clip(np.rint(cv2.cvtColor(lab, cv2.COLOR_Lab2BGR) * 255.0), 0, 255).astype(np.uint8)
        self.assertTrue({"colour_oversaturated", "colour_cast", "lightness_changed"} & set(self.verdict(self.parent, vivid)["reasons"]))

    def test_the_limits_are_documented_numbers(self):
        limits = Limits()
        self.assertEqual((limits.colourize_luminance_psnr_min, limits.colourize_mean_chroma_max, limits.colourize_p99_chroma_max, limits.colourize_cast_max), (38.0, 28.0, 58.0, 26.0))


class NaturalColourTests(SimpleTestCase):
    def test_the_factor_damps_everywhere_more_in_the_dark_and_in_the_highlights(self):
        lightness = np.array([0.0, 8.0, 35.0, 60.0, 80.0, 96.0, 100.0], np.float32)
        factor = colorize.natural_chroma_factor(lightness, scale=0.7, dark=0.3, highlight=0.5)
        np.testing.assert_allclose(factor, [0.21, 0.21, 0.7, 0.7, 0.7, 0.35, 0.35], atol=1e-5)

    def test_the_raw_prediction_setting_is_one_zero_one(self):
        np.testing.assert_allclose(colorize.natural_chroma_factor(np.array([5.0, 50.0, 99.0], np.float32), scale=1.0, dark=1.0, highlight=0.0), [1.0, 1.0, 1.0])

    def test_the_defaults_are_the_measured_ones(self):
        from django.conf import settings

        self.assertEqual((settings.DDCOLOR_CHROMA_SCALE, settings.DDCOLOR_DARK_DAMPING, settings.DDCOLOR_HIGHLIGHT_DAMPING, settings.DDCOLOR_MAX_CHROMA, settings.DDCOLOR_MODEL_VARIANT),
                         (0.7, 0.3, 0.5, 60.0, "large"))
        self.assertIs(settings.DDCOLOR_ENABLED, False)


class DDColorAdapterSafetyTests(SimpleTestCase):
    """The adapter refuses what it cannot run safely, before any network is built."""

    def setUp(self):
        colorize._MODEL_CACHE.clear()

    def test_a_bad_input_is_refused(self):
        for bad in (None, np.zeros((10, 10), np.uint8), np.zeros((10, 10, 3), np.float32)):
            with self.assertRaises(colorize.DDColorUnavailable):
                colorize.colorize_with_ddcolor(bad)

    def test_a_missing_checkpoint_is_unavailable(self):
        with override_settings(DDCOLOR_MODEL_PATH="/nonexistent/ddcolor.bin"), self.assertRaises(colorize.DDColorUnavailable):
            colorize.colorize_with_ddcolor(np.zeros((10, 10, 3), np.uint8))

    def test_a_checkpoint_that_does_not_match_its_pinned_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ddcolor.bin"
            path.write_bytes(b"not the pinned weights")
            with override_settings(DDCOLOR_MODEL_PATH=str(path), DDCOLOR_MODEL_SHA256="0" * 64), self.assertRaises(colorize.DDColorUnavailable) as caught:
                colorize.colorize_with_ddcolor(np.zeros((10, 10, 3), np.uint8))
        self.assertIn("integrity", str(caught.exception))

    @NEEDS_TORCH
    def test_a_checkpoint_that_only_partly_fits_the_network_is_refused_not_run_as_a_random_network(self):
        import torch

        class Tiny(torch.nn.Module):
            def __init__(self, **_ignored):
                super().__init__()
                self.layer = torch.nn.Linear(2, 2)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ddcolor.bin"
            torch.save({"layer.weight": torch.zeros(2, 2)}, path)         # no "layer.bias": strict=False would load it silently
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with override_settings(DDCOLOR_MODEL_PATH=str(path), DDCOLOR_MODEL_SHA256=digest), patch("enhancement.archs.ddcolor.model.DDColor", Tiny), \
                    self.assertRaises(colorize.DDColorUnavailable) as caught:
                colorize.colorize_with_ddcolor(np.zeros((10, 10, 3), np.uint8))
        self.assertIn("does not match", str(caught.exception))

    @NEEDS_TORCH
    def test_the_output_keeps_the_input_lightness_and_size_with_a_stand_in_network(self):
        import torch

        class Predicts(torch.nn.Module):
            def __init__(self, **_ignored):
                super().__init__()
                self.p = torch.nn.Parameter(torch.zeros(1))

            def forward(self, x):                      # a constant warm prediction (a=20, b=40) at the input size
                out = torch.zeros(x.shape[0], 2, x.shape[2], x.shape[3])
                out[:, 0], out[:, 1] = 20.0, 40.0
                return out + self.p

        source = grey(base.portrait())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ddcolor.bin"
            torch.save({"p": torch.zeros(1)}, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with override_settings(DDCOLOR_MODEL_PATH=str(path), DDCOLOR_MODEL_SHA256=digest, DDCOLOR_DEVICE="cpu"), patch("enhancement.archs.ddcolor.model.DDColor", Predicts):
                out, metadata = colorize.colorize_with_ddcolor(source)
        self.assertEqual(out.shape, source.shape)
        self.assertGreater(cv2.PSNR(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY), cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)), 38.0)
        lab = cv2.cvtColor(out.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
        self.assertLessEqual(float(np.hypot(lab[:, :, 1], lab[:, :, 2]).max()), 60.5, "capped at DDCOLOR_MAX_CHROMA")
        self.assertGreater(float(np.hypot(lab[:, :, 1], lab[:, :, 2]).mean()), 5.0)
        self.assertEqual((metadata["provider"], metadata["chroma_scale"]), ("ddcolor_large", 0.7))


class DarkSceneAnalyzer(base.ImageQualityAnalyzer):
    """The analyzer, but it reads every photograph as a deliberately dark scene (no defect recommended)."""

    def analyze(self, image):
        report = super().analyze(image)
        report.exposure = {**report.exposure, "status": "dark_scene"}
        report.recommended_operations = []
        return report


def refused_lift(image, engine, variant):
    return image.copy(), [{"model": engine}]          # nothing gets brighter: the validator refuses it


@override_settings(**dict(base.ALL_ON, ADAPTIVE_POLISH_ENABLED=True))
class RefusedAttemptDoesNotBlockPolishTests(SimpleTestCase):
    def stages(self, analyzer, style, image=None, level="auto"):
        service = AdaptiveImageEnhancementService(analyzer=analyzer, config=EngineConfig(), runners={ops.LOW_LIGHT: refused_lift})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
            _out, _level, parameters = service.enhance(image if image is not None else base.portrait(), requested_level=level, style=style)
        return {stage["operation"]: stage for stage in parameters["adaptive"]["stages"]}

    def test_ai_max_still_polishes_after_its_own_refused_dark_scene_lift(self):
        stages = self.stages(DarkSceneAnalyzer(detector=base.StubDetector([])), "ai_max")
        self.assertEqual(stages[ops.LOW_LIGHT]["status"], "rejected")
        self.assertTrue(stages[ops.LOW_LIGHT]["optional"])
        self.assertIn(ops.POLISH, stages, "the strongest style must do at least what Dynamic does")

    def test_natural_leaves_a_dark_scene_entirely_alone(self):
        self.assertEqual(self.stages(DarkSceneAnalyzer(detector=base.StubDetector([])), "natural"), {})

    def test_a_refused_lift_that_a_real_defect_called_for_still_blocks_polish_as_before(self):
        stages = self.stages(base.analyzer_with(), "ai_max", image=base.portrait(brightness=0.12))
        self.assertEqual(stages[ops.LOW_LIGHT]["status"], "rejected")
        self.assertFalse(stages[ops.LOW_LIGHT]["optional"])
        self.assertNotIn(ops.POLISH, stages)

    def test_the_step_says_when_it_was_asked_for_by_the_profile(self):
        router = AdaptiveEnhancementRouter(EngineConfig())
        report = DarkSceneAnalyzer(detector=base.StubDetector([])).analyze(base.portrait())
        self.assertTrue(router.next_step(report, set(), None, profile_for("ai_max")).step.optional)
        defective = base.analyzer_with().analyze(base.portrait(brightness=0.12))
        self.assertFalse(router.next_step(defective, set(), None, profile_for("ai_max")).step.optional)

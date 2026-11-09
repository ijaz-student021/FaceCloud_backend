"""Detail restoration: a soft or heavily compressed photograph (a WhatsApp copy) is made clearer at its own size (adaptive/operations.run_detail), and the optional
4x-UltraSharp checkpoint (hd_restoration.convert_esrgan_keys). Stand-ins replace the networks here (CI has no weights); the real models were run on the owner's
1600 x 1382 portrait, see docs/evaluation/2026-11-03-detail-restoration.md.
"""
import importlib.util
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import hd_restoration
from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator, Limits
from .test_adaptive_polish import _no_identity

ON = dict(base.ALL_ON, HD_RESTORATION_ENABLED=True, ADAPTIVE_DETAIL_ENABLED=True, ULTRASHARP_ENABLED=False)
BEFORE_DETAIL = {ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR, ops.FACE}      # what the engine has attempted when it gets to detail restoration (an upscale only if it was offered)


def sharpened(image, amount=0.6):
    blurred = cv2.GaussianBlur(image, (0, 0), 1.6)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


class HeavyCompressionAnalyzer(ImageQualityAnalyzer):
    """Reads the ORIGINAL as softly blurred and heavily compressed and anything else as clean, so a candidate that "fixes" it can be judged."""

    def __init__(self, original, **kwargs):
        super().__init__(**kwargs)
        self.original = original

    def analyze(self, image):
        report = super().analyze(image)
        if image is self.original or np.array_equal(image, self.original):
            report.compression = {"status": "heavy", "blockiness": [2.0]}
            report.blur = {**report.blur, "status": "low", "type": "defocus", "retention": 0.55}
        else:
            report.compression = {"status": "none", "blockiness": [0.5]}
            report.blur = {**report.blur, "status": "none", "type": "none", "retention": 0.45}
        return report


@override_settings(**ON, ADAPTIVE_ALWAYS_ENHANCE=False)
class DetailRouterTests(SimpleTestCase):
    def router(self):
        return AdaptiveEnhancementRouter(EngineConfig())

    def report(self, compression="heavy", blur="none", size=None, **changes):
        image = base.portrait() if size is None else np.full((size[1], size[0], 3), 120, np.uint8)
        report = base.analyzer_with(None if size else (150, 100, 180, 220)).analyze(image)
        report.compression = {"status": compression, "blockiness": [2.0 if compression == "heavy" else 0.5]}
        report.blur = {**report.blur, "status": blur, "type": "defocus" if blur != "none" else "none"}
        report.noise = {**report.noise, "status": "none"}
        for name, value in changes.items():
            setattr(report, name, value)
        return report

    def step(self, report, profile=None, done=BEFORE_DETAIL):
        return self.router().next_step(report, set(done), None, profile).step

    def test_heavy_compression_or_softness_is_offered_detail_restoration(self):
        blurred_face = {"status": "high", "detail": 20.0}
        for report in (self.report("heavy", "none"), self.report("none", "low", face_blur=blurred_face), self.report("heavy", "high")):
            step = self.step(report)
            self.assertEqual((step.operation, step.engines, step.optional), (ops.DETAIL, ["general", "realesrgan"], True))
            self.assertEqual([v["blend"] for v in step.variants], [1.0, 0.65])

    def test_a_clean_sharp_photograph_is_left_alone(self):
        self.assertIsNone(self.step(self.report("none", "none")))

    def test_the_strength_sets_the_blends(self):
        self.assertEqual([v["blend"] for v in self.step(self.report(), profile_for("natural", "low")).variants], [0.65, 0.4])

    @override_settings(ULTRASHARP_ENABLED=True)
    def test_the_fast_model_is_first_and_ultrasharp_only_leads_for_ai_max_or_a_strong_level(self):
        self.assertEqual(self.step(self.report()).engines, ["general", "realesrgan", "ultrasharp"])
        big = self.report(size=(1500, 1100))           # over the sizes ai_max and Strong enlarge, so the step is detail restoration and not an enlargement
        for profile in (profile_for("ai_max"), profile_for("natural", "high"), profile_for("dynamic", "high")):
            self.assertEqual(self.step(big, profile).engines, ["ultrasharp", "general", "realesrgan"], profile.describe())
        self.assertEqual(self.step(big, profile_for("dynamic")).engines[0], "general")

    @override_settings(HD_RESTORATION_ENABLED=False, ULTRASHARP_ENABLED=True)
    def test_ultrasharp_alone_is_still_used_when_the_fast_models_are_off(self):
        self.assertEqual(self.step(self.report()).engines, ["ultrasharp"])

    def test_a_sharp_face_in_front_of_a_defocused_background_is_not_soft(self):
        """A professional portrait: the frame reads blurred (bokeh), the face is sharp. It wasted two minutes of model time before the validator refused it."""
        report = self.report("none", "low", face_blur={"status": "none", "detail": 150.0})
        self.assertIsNotNone(report.main_face)
        self.assertIsNone(self.step(report))
        self.assertIsNotNone(self.step(self.report("none", "low", face_blur={"status": "high", "detail": 20.0})), "a blurred face is")
        self.assertIsNotNone(self.step(self.report("heavy", "low", face_blur={"status": "none", "detail": 150.0})), "heavy compression is a reason on its own")

    @override_settings(ADAPTIVE_DETAIL_ENABLED=False)
    def test_it_can_be_switched_off(self):
        self.assertIsNone(self.step(self.report()))

    @override_settings(HD_RESTORATION_ENABLED=False)
    def test_without_a_model_the_reason_is_recorded(self):
        decision = self.router().next_step(self.report(), set(BEFORE_DETAIL))
        self.assertIsNone(decision.step)
        self.assertIn({"operation": ops.DETAIL, "reason": "No detail-restoration model is switched on."}, decision.skipped)

    def test_a_small_photograph_is_enlarged_not_restored_a_second_time(self):
        report = self.report(resolution={"status": "low", "short_edge": 300, "face_short_edge": None})
        self.assertEqual(self.step(report).operation, ops.UPSCALE)                              # the enlargement is what it is offered
        self.assertIsNone(self.step(report, done=BEFORE_DETAIL | {ops.UPSCALE}))              # and after it, detail restoration is not run again
        self.assertIsNone(self.step(self.report(), done=BEFORE_DETAIL | {ops.UPSCALE}))

    @override_settings(ADAPTIVE_DETAIL_MAX_PIXELS=100_000)
    def test_a_photograph_over_the_pixel_budget_is_left_alone_and_says_why(self):
        decision = self.router().next_step(self.report(size=(640, 480)), set(BEFORE_DETAIL))
        self.assertIsNone(decision.step)
        self.assertTrue(any(item["operation"] == ops.DETAIL and "too large" in item["reason"] for item in decision.skipped), decision.skipped)

    def test_detail_comes_before_polish_and_after_the_defect_operations(self):
        self.assertEqual(ops.ORDER[-4:], (ops.UPSCALE, ops.DETAIL, ops.POLISH, ops.COLORIZE))
        self.assertEqual(ops.ENGINES[ops.DETAIL], ("ultrasharp", "general", "realesrgan"))


class DetailValidatorTests(SimpleTestCase):
    def verdict(self, parent, candidate, before=("heavy", "low"), after=("none", "none")):
        analyzer = base.analyzer_with(None)
        parent_report, candidate_report = analyzer.analyze(parent), analyzer.analyze(candidate)
        for report, (compression, blur) in ((parent_report, before), (candidate_report, after)):
            report.compression = {"status": compression, "blockiness": [1.0]}
            report.blur = {**report.blur, "status": blur, "retention": 0.55 if blur != "none" else 0.45}
        validator = FinalImageQualityValidator(analyzer, parent_report)
        return validator.evaluate(parent, candidate, ops.DETAIL, parent_report, candidate_report=candidate_report)

    def setUp(self):
        self.parent = base.portrait()

    def test_a_clearer_photograph_that_lost_its_defects_is_accepted(self):
        verdict = self.verdict(self.parent, sharpened(self.parent, 0.25))
        self.assertTrue(verdict["accepted"], (verdict["reasons"], verdict["gates"].get("improves_defect")))
        self.assertGreater(verdict["improvement"], 0.0)
        self.assertLessEqual(verdict["improvement"], 1.0)

    def test_an_unchanged_photograph_adds_nothing_and_is_refused(self):
        self.assertIn("no_added_detail_or_invented_detail", self.verdict(self.parent, self.parent.copy())["reasons"])

    def test_an_over_sharpened_photograph_is_refused(self):
        self.assertFalse(self.verdict(self.parent, sharpened(self.parent, 4.0))["accepted"])

    def test_more_structure_without_any_defect_resolved_is_refused(self):
        verdict = self.verdict(self.parent, sharpened(self.parent, 0.25), before=("none", "none"), after=("none", "none"))
        self.assertIn("no_added_detail_or_invented_detail", verdict["reasons"])

    def test_the_limits_are_documented_numbers(self):
        self.assertEqual((Limits().detail_min_gain, Limits().detail_max_gain), (1.02, 1.6))


@override_settings(**dict(ON, ADAPTIVE_POLISH_ENABLED=True), ADAPTIVE_ALWAYS_ENHANCE=False)
class DetailEngineTests(SimpleTestCase):
    def run_engine(self, style="natural", level="auto", runner=None):
        source = base.portrait()
        analyzer = HeavyCompressionAnalyzer(source, detector=base.StubDetector([]))
        calls = []

        def default_runner(image, engine, variant):
            calls.append((engine, variant["blend"]))
            return sharpened(image, 0.25), [{"model": engine}]

        service = AdaptiveImageEnhancementService(analyzer=analyzer, config=EngineConfig(), runners={ops.DETAIL: runner or default_runner})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
            out, _level, parameters = service.enhance(source, requested_level=level, style=style)
        return source, out, parameters["adaptive"], calls

    def test_a_soft_compressed_photograph_comes_out_clearer_at_the_same_size(self):
        source, out, adaptive, calls = self.run_engine()
        self.assertEqual(out.shape, source.shape)
        self.assertEqual((adaptive["outcome"], adaptive["operations_applied"]), ("enhanced", [ops.DETAIL]))
        self.assertIn("restored fine detail", adaptive["summary"])
        self.assertGreater(float(np.abs(out.astype(int) - source.astype(int)).mean()), 0.2)
        self.assertEqual([call[1] for call in calls], [1.0, 0.65])

    def test_polish_is_not_stacked_on_a_restored_photograph(self):
        _source, _out, adaptive, _calls = self.run_engine("dynamic")
        self.assertEqual([stage["operation"] for stage in adaptive["stages"]], [ops.DETAIL])

    @staticmethod
    def missing_model(image, engine, variant):
        raise hd_restoration.HDRestorationUnavailable("The Real-ESRGAN checkpoint is unavailable.")

    @override_settings(ADAPTIVE_POLISH_ENABLED=False)
    def test_a_missing_model_leaves_the_photograph_as_it_was_and_says_why(self):
        missing = self.missing_model
        source, out, adaptive, _calls = self.run_engine(runner=missing)
        np.testing.assert_array_equal(out, source)
        stage = next(s for s in adaptive["stages"] if s["operation"] == ops.DETAIL)
        self.assertEqual((stage["status"], stage["skipped_engines"][0]["reason"]), ("unavailable", "The Real-ESRGAN checkpoint is unavailable."))


    def test_a_missing_or_refused_detail_attempt_does_not_stop_the_polish(self):
        stages = [stage["operation"] for stage in self.run_engine(runner=self.missing_model)[2]["stages"]]
        self.assertEqual(stages[0], ops.DETAIL)
        self.assertIn(ops.POLISH, stages, "the photograph is as it was, so the gentle polish is still tried")

    def test_an_accepted_detail_restoration_does_stop_the_polish(self):
        self.assertEqual([s["operation"] for s in self.run_engine("dynamic")[2]["stages"]], [ops.DETAIL])


class DetailModelOutputIsComputedOnceTests(SimpleTestCase):
    def setUp(self):
        ops.clear_detail_cache()
        self.addCleanup(ops.clear_detail_cache)
        self.calls = []

    def fake(self, name):
        def upscale(image, output_scale=None):
            self.calls.append((name, output_scale))
            return cv2.resize(image, None, fx=output_scale, fy=output_scale, interpolation=cv2.INTER_CUBIC), {"provider": name, "device": "cpu"}
        return upscale

    @override_settings(REALESRGAN_OUTPUT_BLEND=0.35)
    def test_the_blends_of_one_step_share_one_model_run_and_come_back_at_the_same_size(self):
        image = base.portrait()
        with patch("enhancement.hd_restoration.upscale_with_realesrgan", self.fake("realesrgan")):
            first, models = ops.run_detail(image, "realesrgan", 1.0)
            second, _ = ops.run_detail(image, "realesrgan", 0.65)
            ops.run_detail(base.portrait(brightness=0.9), "realesrgan", 1.0)             # another photograph: the model runs again
        self.assertEqual(self.calls, [("realesrgan", 2), ("realesrgan", 2)])
        self.assertEqual((first.shape, second.shape), (image.shape, image.shape))
        self.assertEqual(models[0]["blend"], 0.35)                                       # Real-ESRGAN keeps the engine's own strength
        self.assertEqual(models[0]["supersample"], 2)

    def test_the_compact_model_runs_at_its_own_strength(self):
        image = base.portrait()
        with patch("enhancement.hd_restoration.upscale_with_general", self.fake("general")):
            first, models = ops.run_detail(image, "general", 1.0)
            _second, gentler = ops.run_detail(image, "general", 0.65)
        self.assertEqual((self.calls, first.shape, models[0]["blend"], gentler[0]["blend"]), ([("general", 2)], image.shape, 0.45, 0.293))

    def test_ultrasharp_runs_at_its_own_full_strength(self):
        image = base.portrait()
        with patch("enhancement.hd_restoration.upscale_with_ultrasharp", self.fake("ultrasharp")):
            _out, models = ops.run_detail(image, "ultrasharp", 1.0)
        self.assertEqual((self.calls, models[0]["blend"], models[0]["model"]), ([("ultrasharp", 2)], 1.0, "ultrasharp"))

    def test_the_cache_is_cleared_after_a_run(self):
        image = base.portrait()
        with patch("enhancement.hd_restoration.upscale_with_realesrgan", self.fake("realesrgan")):
            ops.run_detail(image, "realesrgan", 1.0)
        self.assertIs(ops._DETAIL_OUTPUT["image"], image)
        ops.clear_detail_cache()
        self.assertIsNone(ops._DETAIL_OUTPUT["image"])


class UltraSharpCheckpointTests(SimpleTestCase):
    def test_the_original_esrgan_keys_are_renamed_to_this_projects_network(self):
        keys = ["model.0.weight", "model.0.bias", "model.1.sub.0.RDB1.conv1.0.weight", "model.1.sub.22.RDB3.conv5.0.bias", "model.1.sub.23.weight", "model.3.weight",
                "model.6.bias", "model.8.weight", "model.10.bias"]
        converted = hd_restoration.convert_esrgan_keys({key: index for index, key in enumerate(keys)})
        self.assertEqual(sorted(converted), sorted(["conv_first.weight", "conv_first.bias", "body.0.rdb1.conv1.weight", "body.22.rdb3.conv5.bias", "conv_body.weight",
                                                    "conv_up1.weight", "conv_up2.bias", "conv_hr.weight", "conv_last.bias"]))
        self.assertEqual(converted["body.22.rdb3.conv5.bias"], 3)

    def test_a_key_of_another_network_is_refused_not_half_loaded(self):
        for bad in ("model.1.sub.0.RDB1.conv1.1.weight", "model.2.weight", "body.0.rdb1.conv1.weight", "model.0.running_mean"):
            with self.assertRaises(ValueError, msg=bad):
                hd_restoration.convert_esrgan_keys({bad: 1})

    def test_it_is_off_by_default_pinned_and_listed_as_non_commercial_for_production(self):
        from django.conf import settings

        from config.production_guard import model_rule

        self.assertIs(settings.ULTRASHARP_ENABLED, False)
        self.assertEqual(settings.ULTRASHARP_MODEL_SHA256, "a5812231fc936b42af08a5edba784195495d303d5b3248c24489ef0c4021fe01")
        problems = model_rule({"ULTRASHARP_ENABLED": True, "NONCOMMERCIAL_MODELS_ACKNOWLEDGED": False})
        self.assertTrue(any("4x-UltraSharp" in problem for problem in problems), problems)
        self.assertFalse(any("4x-UltraSharp" in problem for problem in model_rule({"ULTRASHARP_ENABLED": False})))


@unittest.skipUnless(importlib.util.find_spec("torch"), "PyTorch is not installed (CI)")
class CompactNetworkTests(SimpleTestCase):
    def test_the_compact_network_has_the_layout_of_the_released_checkpoint(self):
        import torch
        from torch import nn
        from torch.nn import functional

        model = hd_restoration._build_srvgg(nn, functional)
        names = list(model.state_dict())
        self.assertEqual((len(names), names[0], names[-1]), (101, "body.0.weight", "body.66.bias"))        # the keys of realesr-general-x4v3.pth, so it loads strictly
        self.assertEqual(tuple(model.state_dict()["body.66.weight"].shape), (48, 64, 3, 3))
        with torch.no_grad():
            self.assertEqual(tuple(model.eval()(torch.zeros(1, 3, 8, 6)).shape), (1, 3, 32, 24))           # 4x

    def test_a_checkpoint_of_another_layout_is_refused_not_half_loaded(self):
        import tempfile
        from pathlib import Path

        import torch

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "general.pth"
            torch.save({"params": {"body.0.weight": torch.zeros(64, 3, 3, 3)}}, path)
            import hashlib

            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with override_settings(REALESRGAN_GENERAL_MODEL_PATH=str(path), REALESRGAN_GENERAL_MODEL_SHA256=digest, HD_RESTORATION_DEVICE="cpu"), \
                    self.assertRaises(hd_restoration.HDRestorationUnavailable):
                hd_restoration._MODEL_CACHE.clear()
                hd_restoration._load_model("realesrgan_general")

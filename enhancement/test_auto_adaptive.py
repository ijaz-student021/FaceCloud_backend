"""Strength "Automatic": the adaptive engine, HD, and a plan that follows what is wrong with THIS photograph.

Stand-in models are used (the engine's decisions are what is tested); real-model behaviour on real photographs is measured by
scripts/evaluate_auto.py and recorded in docs/evaluation/. Each fixture prints its plan so a failing run shows what was decided.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import restoration
from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive import strength
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .services import enhance_bgr

AUTO = dict(base.ALL_ON, ENHANCEMENT_AUTO_ADAPTIVE=True, ADAPTIVE_HD_TARGET_LONG_EDGE=1920, ADAPTIVE_HD_FALLBACK_LANCZOS=True,
            ADAPTIVE_TONE_ENABLED=True, CODEFORMER_ENABLED=False, GFPGAN_ENABLED=False, ADAPTIVE_TIME_BUDGET_SECONDS=120.0)


class ScaledStubDetector(base.StubDetector):
    """The same face at the same place in the frame whatever the image size (an enlarged photograph has its face enlarged too)."""

    def __init__(self, box, reference_width):
        super().__init__([(box, base.landmarks_for(box))])
        self.box, self.reference_width = box, reference_width

    def detect(self, image):
        factor = image.shape[1] / self.reference_width
        box = tuple(int(round(v * factor)) for v in self.box)
        self.faces = [(box, base.landmarks_for(box))]
        return super().detect(image)


def analyzer_for(box=(300, 150, 220, 280), reference_width=960):
    return base.ImageQualityAnalyzer(detector=ScaledStubDetector(box, reference_width))


def unsharp(image, amount=0.8, sigma=1.4):
    return cv2.addWeighted(image, 1 + amount, cv2.GaussianBlur(image, (0, 0), sigma), -amount, 0)


def runners():
    """Stand-ins that do what the real model would, roughly: lift, smooth, sharpen, enlarge with a little extra detail."""
    def lift(image, engine, variant):
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)   # lightness gain, chroma raised by its square root (colour kept)
        lab[:, :, 0] = np.clip(lab[:, :, 0] * 3.0, 0, 255)
        lab[:, :, 1:] = 128 + (lab[:, :, 1:] - 128) * 3.0 ** 0.5
        lifted = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
        return cv2.addWeighted(lifted, variant["blend"], image, 1 - variant["blend"], 0), [{"model": engine}]

    def denoise(image, engine, variant):
        smooth = cv2.fastNlMeansDenoisingColored(image, None, 10, 10, 5, 15)
        return cv2.addWeighted(smooth, variant["blend"], image, 1 - variant["blend"], 0), [{"model": engine}]

    def deblur(image, engine, variant):
        return cv2.addWeighted(unsharp(image, 1.2), variant["blend"], image, 1 - variant["blend"], 0), [{"model": engine}]

    def upscale(image, engine, variant):
        big = cv2.resize(image, (image.shape[1] * variant["scale"], image.shape[0] * variant["scale"]), interpolation=cv2.INTER_CUBIC)
        return unsharp(big, 0.35, 1.0), [{"model": engine}]

    return {ops.LOW_LIGHT: lift, ops.DENOISE: denoise, ops.DEBLUR: deblur, ops.UPSCALE: upscale}


def service(face=True, **config):
    cfg = EngineConfig.from_settings(auto=True)
    for name, value in config.items():
        setattr(cfg, name, value)
    return AdaptiveImageEnhancementService(analyzer=base.analyzer_with() if face else base.analyzer_with(None), config=cfg, runners=runners())


def fixtures():
    """Six photographs with different problems (generated; no photographs of people)."""
    clean = base.portrait(size=(720, 960), face_box=(300, 150, 220, 280))
    return {
        "dim": base.portrait(size=(720, 960), brightness=0.22, face_box=(300, 150, 220, 280)),
        "noisy": base.portrait(size=(720, 960), noise=26, face_box=(300, 150, 220, 280)),
        "motion_blur": base.motion_blur(clean, 25, 10),
        "defocus_blur": cv2.GaussianBlur(clean, (0, 0), 6.0),
        "low_res_240": cv2.resize(clean, (320, 240), interpolation=cv2.INTER_AREA),
        "clean": clean,
    }


FACE_FOR = {"low_res_240": ((100, 50, 73, 93), 320)}


def plan_of(name, image):
    face_box, width = FACE_FOR.get(name, ((300, 150, 220, 280), 960))
    svc = AdaptiveImageEnhancementService(analyzer=analyzer_for(face_box, width), config=EngineConfig.from_settings(auto=True), runners=runners())
    output, level, parameters = svc.enhance(image, allow_biometric_processing=False)
    return output, parameters["adaptive"]


class StrengthFunctionTests(SimpleTestCase):
    def report(self, **overrides):
        report = base.analyzer_with().analyze(base.portrait())
        for name, value in overrides.items():
            setattr(report, name, value)
        return report

    def test_the_lift_grows_as_the_face_gets_darker(self):
        c = strength.StrengthConfig.from_settings()
        blends = []
        for brightness in (0.45, 0.3, 0.2, 0.1):
            report = base.analyzer_with().analyze(base.portrait(brightness=brightness))
            blends.append(strength.lowlight(report, c)[0])
        self.assertEqual(blends, sorted(blends))
        self.assertLess(blends[0], blends[-1])
        self.assertGreaterEqual(blends[0], c.lowlight_blend_min)
        self.assertLessEqual(blends[-1], 1.0)

    def test_the_denoise_grows_with_the_noise_level(self):
        c = strength.StrengthConfig.from_settings()
        values = [strength.denoise(self.report(noise={"level": level, "status": "x"}), c)[0] for level in (0.9, 1.5, 2.0, 3.0)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[-1], values[0])
        self.assertEqual(strength.denoise(self.report(noise={"level": 9.0, "status": "x"}), c)[0], 1.0)

    def test_deblur_and_face_fidelity_follow_how_blurred_the_face_is(self):
        c = strength.StrengthConfig.from_settings()
        details = (22.0, 12.0, 4.0)
        reports = [self.report(face_blur={"status": "high", "detail": d, "judged_on": "main face"}) for d in details]
        blends = [strength.deblur(r, c)[0] for r in reports]
        self.assertEqual(blends, sorted(blends))
        self.assertLess(blends[0], blends[-1])
        fidelities = []
        for detail in (70.0, 40.0, 10.0):
            report = base.analyzer_with().analyze(base.portrait())
            report.faces[0] = type(report.faces[0])(**{**report.faces[0].__dict__, "detail": detail})
            fidelities.append(strength.face_fidelity(report, c)[0])
        self.assertEqual(fidelities, sorted(fidelities, reverse=True), "a more degraded face is restored more boldly (lower w)")
        self.assertGreaterEqual(min(fidelities), c.face_fidelity_min)
        self.assertLessEqual(max(fidelities), c.face_fidelity_max)

    def test_the_tone_pass_is_gentle_on_contrast_and_firmer_on_a_flat_photograph_but_never_zero(self):
        c = strength.StrengthConfig.from_settings()
        low = strength.tone(self.report(base={"contrast_stddev": 20.0}), c)[0]
        high = strength.tone(self.report(base={"contrast_stddev": 70.0}), c)[0]
        self.assertGreater(low, high)
        self.assertEqual(high, c.tone_strength_min)
        self.assertGreater(high, 0)

    def test_the_styles_only_bias_the_strengths(self):
        natural = strength.StrengthConfig.from_settings("natural")
        dynamic = strength.StrengthConfig.from_settings("dynamic")
        ai_max = strength.StrengthConfig.from_settings("ai_max")
        self.assertGreater(dynamic.tone_strength_min, natural.tone_strength_min)
        self.assertGreater(ai_max.deblur_blend_min, natural.deblur_blend_min)
        self.assertLess(ai_max.face_fidelity_min, natural.face_fidelity_min)
        self.assertEqual(strength.StrengthConfig.from_settings("anything-else"), natural)

    def test_the_smallest_useful_scale_reaches_hd_within_the_limit(self):
        self.assertEqual(strength.upscale_scale(1280, 1920, 4096)[:2], (2, True))
        self.assertEqual(strength.upscale_scale(960, 1920, 4096)[:2], (2, True))
        self.assertEqual(strength.upscale_scale(640, 1920, 4096)[:2], (4, True))
        self.assertEqual(strength.upscale_scale(320, 1920, 4096)[:2], (4, False), "x4 is the most one pass can do")
        self.assertEqual(strength.upscale_scale(1920, 1920, 4096)[:2], (0, True))
        self.assertEqual(strength.upscale_scale(4000, 1920, 4096)[:2], (0, True), "a source at or above HD keeps its size")
        self.assertEqual(strength.upscale_scale(1500, 1920, 2000)[0], 0, "x2 would exceed the output limit")
        self.assertEqual(strength.upscale_scale(1100, 1920, 2400)[:2], (2, True))


@override_settings(**AUTO)
class AutoPlanTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plans = {}
        with override_settings(**AUTO):
            for name, image in fixtures().items():
                output, adaptive = plan_of(name, image)
                cls.plans[name] = (image, output, adaptive)
                steps = [f"{op['stage']}[{op['status']}] strength={op['strength']} engine={op['engine']}" for op in adaptive["plan"]["operations"]]
                print(f"\n[auto plan] {name}: {adaptive['plan']['hd']['output_size']} <- {image.shape[1]}x{image.shape[0]}; " + "; ".join(steps))

    def stages(self, name):
        return {op["stage"]: op for op in self.plans[name][2]["plan"]["operations"]}

    def test_different_problems_give_different_plans(self):
        plans = {name: tuple(sorted((stage, op["status"], op["strength"]) for stage, op in self.stages(name).items())) for name in self.plans}
        self.assertEqual(len(set(plans.values())), len(plans), "two fixtures with different problems produced the same plan")

    def test_each_problem_gets_its_own_operation(self):
        self.assertIn("low_light", self.stages("dim"))
        self.assertIn("denoise", self.stages("noisy"))
        self.assertIn("deblur", self.stages("motion_blur"))
        self.assertIn("deblur", self.stages("defocus_blur"), "a strongly blurred face is deblurred whatever the frame reads")
        for clean_stage in ("low_light", "denoise", "deblur"):
            self.assertNotIn(clean_stage, self.stages("clean"))
        for name in ("dim", "noisy", "motion_blur", "defocus_blur", "low_res_240", "clean"):
            self.assertNotIn("face_restore", {s for s, op in self.stages(name).items() if op["status"] == "accepted"}, name)

    def test_the_strengths_are_measured_not_fixed(self):
        dim = self.stages("dim")["low_light"]
        self.assertIsNotNone(dim["planned_strength"])
        self.assertGreater(dim["measure"]["deficit"], 0.3)
        noisy = self.stages("noisy")["denoise"]
        self.assertGreater(noisy["measure"]["noise_level"], 1.0)
        darker = base.portrait(size=(720, 960), brightness=0.1, face_box=(300, 150, 220, 280))
        _, darker_plan = plan_of("dim", darker)
        darker_strength = next(op for op in darker_plan["plan"]["operations"] if op["stage"] == "low_light")["planned_strength"]
        self.assertGreater(darker_strength, dim["planned_strength"], "a darker photograph gets a stronger lift")

    def test_every_output_is_hd_or_says_why_not(self):
        for name, (image, output, adaptive) in self.plans.items():
            hd = adaptive["plan"]["hd"]
            long_edge = max(output.shape[:2])
            self.assertEqual(hd["output_long_edge"], long_edge, name)
            if max(image.shape[:2]) < 1920:
                self.assertGreater(long_edge, max(image.shape[:2]), name)
            self.assertEqual(hd["reached"], long_edge >= 1920, name)
        self.assertTrue(self.plans["dim"][2]["plan"]["hd"]["reached"])
        self.assertFalse(self.plans["low_res_240"][2]["plan"]["hd"]["reached"], "320 px cannot reach 1920 with one x4 pass")
        self.assertIn("HD", self.plans["low_res_240"][2]["summary"])

    def test_a_clean_well_exposed_photograph_is_still_visibly_improved(self):
        image, output, adaptive = self.plans["clean"]
        accepted = [op["stage"] for op in adaptive["plan"]["operations"] if op["status"] == "accepted"]
        self.assertIn("upscale", accepted)
        self.assertIn("tone", accepted)
        self.assertGreater(output.shape[1], image.shape[1])
        reference = cv2.resize(image, (output.shape[1], output.shape[0]), interpolation=cv2.INTER_CUBIC)
        self.assertGreater(float(np.abs(output.astype(np.int16) - reference.astype(np.int16)).mean()), 0.5)
        self.assertEqual(adaptive["outcome"], "enhanced")

    def test_the_summary_is_a_plain_sentence_built_from_the_plan_and_never_empty(self):
        for name, (_image, _output, adaptive) in self.plans.items():
            self.assertGreater(len(adaptive["summary"]), 20, name)
            self.assertTrue(adaptive["summary"].endswith("."), name)
        self.assertIn("brightened", self.plans["dim"][2]["summary"])
        self.assertIn("reduced noise", self.plans["noisy"][2]["summary"])

    def test_the_diagnosis_carries_the_measurements_with_their_status_words(self):
        diagnosis = self.plans["dim"][2]["diagnosis"]
        self.assertEqual(diagnosis["exposure"]["status"], "low_light_subject")
        self.assertIn("level", diagnosis["noise"])
        self.assertIn("status", diagnosis["face_blur"])
        self.assertEqual(diagnosis["size"], [960, 720])
        self.assertIn("low_light", diagnosis["recommended_operations"])


@override_settings(**AUTO)
class AutoGuardrailTests(SimpleTestCase):
    def test_a_stage_that_cannot_run_is_named_in_the_summary(self):
        with override_settings(NAFNET_DEBLUR_ENABLED=False):
            _output, adaptive = plan_of("motion_blur", fixtures()["motion_blur"])
        self.assertIn("Deblurring was needed but is not available on this server.", adaptive["summary"])
        self.assertTrue(any(item["stage"] == "deblur" for item in adaptive["plan"]["skipped"]))

    def test_hd_falls_back_to_a_conventional_filter_and_says_so(self):
        failing = runners()

        def broken(image, engine, variant):
            from .hd_restoration import HDRestorationUnavailable

            raise HDRestorationUnavailable("The Real-ESRGAN checkpoint is unavailable.")

        failing[ops.UPSCALE] = lambda image, engine, variant: broken(image, engine, variant) if engine == "realesrgan" else ops.run_upscale(image, engine, variant["scale"])
        svc = AdaptiveImageEnhancementService(analyzer=analyzer_for(), config=EngineConfig.from_settings(auto=True), runners=failing)
        output, _level, parameters = svc.enhance(fixtures()["clean"], allow_biometric_processing=False)
        adaptive = parameters["adaptive"]
        self.assertGreater(output.shape[1], 960)
        self.assertIn("conventional filter", adaptive["summary"])
        self.assertIn("no new detail", adaptive["summary"])
        upscale = restoration.describe_stages(parameters)["applied_stages"][-1]
        self.assertEqual((upscale["stage"], upscale["engine"]), ("upscale", "lanczos"))
        stage = next(s for s in adaptive["stages"] if s["operation"] == ops.UPSCALE)
        self.assertEqual(stage["skipped_engines"][0]["engine"], "realesrgan")

    def test_without_the_fallback_and_without_the_model_hd_is_not_reached_and_the_summary_says_so(self):
        with override_settings(ADAPTIVE_HD_FALLBACK_LANCZOS=False, HD_RESTORATION_ENABLED=False):
            output, adaptive = plan_of("clean", fixtures()["clean"])
        self.assertEqual(output.shape[1], 960)
        self.assertIn("HD enlargement was needed but is not available on this server.", adaptive["summary"])
        self.assertFalse(adaptive["plan"]["hd"]["reached"])

    def test_an_over_sharpening_candidate_is_rejected_and_recorded(self):
        harsh = runners()
        harsh[ops.DEBLUR] = lambda image, engine, variant: (unsharp(image, 6.0, 1.0), [{"model": engine}])
        svc = AdaptiveImageEnhancementService(analyzer=analyzer_for(), config=EngineConfig.from_settings(auto=True), runners=harsh)
        _output, _level, parameters = svc.enhance(fixtures()["defocus_blur"], allow_biometric_processing=False)
        deblur = next(s for s in parameters["adaptive"]["stages"] if s["operation"] == ops.DEBLUR)
        self.assertEqual(deblur["status"], "rejected")
        self.assertTrue(deblur["rejection_reasons"])
        self.assertIn("did not pass the safety checks", parameters["adaptive"]["summary"])

    def test_a_source_at_hd_keeps_its_size(self):
        big = cv2.resize(fixtures()["clean"], (2000, 1500), interpolation=cv2.INTER_CUBIC)
        svc = AdaptiveImageEnhancementService(analyzer=analyzer_for((650, 300, 450, 560), 2000), config=EngineConfig.from_settings(auto=True), runners=runners())
        output, _level, parameters = svc.enhance(big, allow_biometric_processing=False)
        self.assertEqual(output.shape[:2], big.shape[:2])
        self.assertTrue(parameters["adaptive"]["plan"]["hd"]["reached"])
        self.assertNotIn("upscale", [op["stage"] for op in parameters["adaptive"]["plan"]["operations"]])

    def test_the_time_budget_keeps_the_best_completed_result_and_says_what_was_skipped(self):
        svc = service(time_budget_seconds=0.0)
        svc.analyzer = analyzer_for()
        output, _level, parameters = svc.enhance(fixtures()["dim"], allow_biometric_processing=False)
        adaptive = parameters["adaptive"]
        self.assertTrue(any("time budget" in item["reason"] for item in adaptive["plan"]["skipped"]))
        self.assertIn("time budget", adaptive["summary"])
        self.assertEqual(output.shape[:2], fixtures()["dim"].shape[:2])


class StyleTests(SimpleTestCase):
    def test_dynamic_is_punchier_and_ai_max_stronger_than_natural_on_the_same_photograph(self):
        image = fixtures()["defocus_blur"]
        strengths = {}
        for style in ("natural", "dynamic", "ai_max"):
            with override_settings(**AUTO):
                svc = AdaptiveImageEnhancementService(
                    analyzer=analyzer_for(), config=EngineConfig.from_settings(auto=True, style=style), runners=runners(),
                )
                _o, _l, parameters = svc.enhance(image, style=style, allow_biometric_processing=False)
            ops_by_stage = {op["stage"]: op for op in parameters["adaptive"]["plan"]["operations"]}
            strengths[style] = (ops_by_stage["deblur"]["planned_strength"], ops_by_stage["tone"]["planned_strength"])
        self.assertGreater(strengths["ai_max"][0], strengths["natural"][0])
        self.assertGreater(strengths["dynamic"][1], strengths["natural"][1])


class DispatchTests(SimpleTestCase):
    image = base.portrait()

    def run_enhance(self, **kwargs):
        sentinel = (self.image.copy(), "medium", {"adaptive": {"outcome": "enhanced"}})
        with patch("enhancement.adaptive.service.AdaptiveImageEnhancementService.enhance", return_value=sentinel) as adaptive, \
                patch("enhancement.services._enhance_bgr_staged", return_value=(self.image.copy(), "low", {"staged": True})) as staged:
            result = enhance_bgr(self.image, **kwargs)
        return result, adaptive, staged

    @override_settings(ENHANCEMENT_AUTO_ADAPTIVE=True, ENHANCEMENT_RENDER_POLICY="lightweight_v1")
    def test_level_auto_runs_the_adaptive_engine_whatever_the_policy_and_the_style(self):
        for style in ("natural", "dynamic", "ai_max"):
            (_img, _level, parameters), adaptive, staged = self.run_enhance(style=style)
            adaptive.assert_called_once()
            staged.assert_not_called()
            self.assertEqual(parameters["auto_adaptive"], {"used": True, "fallback": None})

    @override_settings(ENHANCEMENT_AUTO_ADAPTIVE=True, ENHANCEMENT_RENDER_POLICY="lightweight_v1")
    def test_a_manual_level_keeps_the_policys_pipeline(self):
        (_img, _level, parameters), adaptive, staged = self.run_enhance(requested_level="medium")
        adaptive.assert_not_called()
        staged.assert_called_once()
        self.assertNotIn("auto_adaptive", parameters)

    @override_settings(ENHANCEMENT_AUTO_ADAPTIVE=False, ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RESTORATION_ESCALATION=False)
    def test_the_switch_returns_automatic_to_the_policys_pipeline(self):
        (_img, _level, _parameters), adaptive, staged = self.run_enhance()
        adaptive.assert_not_called()
        staged.assert_called_once()

    @override_settings(ENHANCEMENT_AUTO_ADAPTIVE=True, ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RESTORATION_ESCALATION=False)
    def test_if_the_engine_cannot_run_the_lightweight_fallback_is_used_and_recorded(self):
        with patch("enhancement.adaptive.service.AdaptiveImageEnhancementService.enhance", side_effect=RuntimeError("boom")), \
                patch("enhancement.services._enhance_bgr_staged", return_value=(self.image.copy(), "low", {"staged": True})):
            _img, _level, parameters = enhance_bgr(self.image)
        self.assertFalse(parameters["auto_adaptive"]["used"])
        self.assertIn("RuntimeError", parameters["auto_adaptive"]["fallback"])
        self.assertIn("lightweight_v1", parameters["auto_adaptive"]["fallback"])


class StagedSummaryTests(SimpleTestCase):
    def test_a_staged_result_always_has_a_sentence(self):
        self.assertIn("No correction was needed", restoration.staged_summary({"plan": {"no_correction_needed": True}}))
        self.assertIn("Adjusted exposure", restoration.staged_summary({"stage_reports": [{"stage": "exposure_enhancement", "changed_pixels": True}]}))
        self.assertIn("could not be enhanced", restoration.staged_summary({}, failed=True))
        self.assertTrue(restoration.staged_summary(None))
        needed = restoration.staged_summary({"restoration_check": {"needed": ["deblur"], "unavailable": {"deblur": "checkpoint not found"}}})
        self.assertIn("checkpoint not found", needed)

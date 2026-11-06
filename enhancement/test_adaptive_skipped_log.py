"""Every time the engine does not use a model, the log says which, for which photograph, which gate said so, what it read and the limit it was held to:

    SKIPPED realesrgan for Photo ID 183 (result 9). Reason: face_geometry failed (face_geometry_changed). Metric: landmark_shift=0.0734, Threshold: 0.06; ...

Found on 2026-11-06: a photograph came back at its own size and the worker log said only ``rejected_for: ["face_geometry_changed"]``. The validator's gates now keep the reading they compared and the
limit they were held to (``metric`` / ``limit``), ``refusal_lines`` words them, and the engine logs one WARNING line per failed gate of a refused candidate, one per model that could not run, and one
per operation the router or the identity check left out. The photograph's label comes from a context variable the task sets (``adaptive/trace.py``), so no signature changes. Stand-in models.
"""
import re
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from . import test_adaptive_lifted_baseline as lifted
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.router import Decision, Step
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.trace import PHOTO, photo
from .adaptive.validator import FinalImageQualityValidator, Limits, refusal_lines
from .hd_restoration import HDRestorationUnavailable
from .services import enhance_bgr
from .tasks import process_enhancement_image
from .test_adaptive_enlargement_geometry import BiasedDetector, clear_enlargement
from .test_adaptive_polish import _no_identity
from .test_pipeline_regressions import NO_MODELS, ResultFixture
from . import test_adaptive_upscale_highlight_guard as guard

LOGGER = "enhancement.adaptive.service"


class TraceTests(SimpleTestCase):
    def test_outside_a_task_the_photograph_is_not_known(self):
        self.assertEqual(PHOTO.get(), "n/a")

    def test_inside_the_context_it_is_the_label_and_afterwards_it_is_not(self):
        with photo("183 (result 9)"):
            self.assertEqual(PHOTO.get(), "183 (result 9)")
        self.assertEqual(PHOTO.get(), "n/a")

    def test_contexts_nest_and_a_failure_inside_does_not_leak_the_label(self):
        with photo("1"):
            with photo("2"):
                self.assertEqual(PHOTO.get(), "2")
            self.assertEqual(PHOTO.get(), "1")
            with self.assertRaises(RuntimeError), photo("3"):
                raise RuntimeError("the model ran out of memory")
            self.assertEqual(PHOTO.get(), "1")
        self.assertEqual(PHOTO.get(), "n/a")


class RefusalLinesTests(SimpleTestCase):
    """The words for each way a gate can fail, from verdicts the real validator makes."""

    def setUp(self):
        self.parent = base.portrait()                                  # 640 rows by 480 columns
        self.analyzer = base.analyzer_with()
        self.report = self.analyzer.analyze(self.parent)
        self.limits = Limits()
        self.validator = FinalImageQualityValidator(self.analyzer, self.report)

    def verdict(self, candidate, operation, scale=1, validator=None):
        return (validator or self.validator).evaluate(self.parent, candidate, operation, self.report, expected_scale=scale)

    def lines(self, candidate, operation, scale=1, validator=None):
        return refusal_lines(self.verdict(candidate, operation, scale, validator))

    def assert_line(self, lines, pattern):
        self.assertTrue(any(re.fullmatch(pattern, line) for line in lines), f"{pattern!r} not in {lines}")

    def test_a_wrong_size(self):
        self.assertEqual(self.lines(cv2.resize(self.parent, (100, 100)), "motion_deblurring"),
                         ["geometry failed (wrong_dimensions). Metric: actual=[100, 100], Threshold: [480, 640]"])

    def test_a_candidate_that_is_not_the_photograph(self):
        self.assert_line(self.lines(255 - self.parent, "motion_deblurring"),
                         rf"data_consistency failed \(content_not_consistent_with_input\)\. Metric: psnr=-?[\d.]+, Threshold: {self.limits.consistency_psnr_min}")

    def test_a_lift_that_changed_the_structure(self):
        self.assert_line(self.lines(255 - self.parent, "low_light_enhancement"),
                         rf"data_consistency failed \(structure_changed\)\. Metric: structure_ssim=-?[\d.]+, Threshold: {self.limits.structure_ssim_min_lowlight}")

    def test_clipped_highlights_against_the_limit_of_the_operation(self):
        candidate = self.parent.copy()
        candidate[:200] = 255
        self.assert_line(self.lines(candidate, "motion_deblurring"), rf"highlights failed \(highlight_clipping_increased\)\. Metric: clipping_growth=[\d.]+, Threshold: {self.limits.highlight_clip_growth_max}")
        self.assert_line(self.lines(candidate, "polish"), rf"highlights failed \(highlight_clipping_increased\)\. Metric: clipping_growth=[\d.]+, Threshold: {self.limits.polish_clip_growth_max}")

    def test_a_halo_against_the_limit_of_a_sharpening_and_of_another_operation(self):
        noisy = np.clip(self.parent.astype(np.int16) + np.random.default_rng(1).integers(-40, 41, self.parent.shape), 0, 255).astype(np.uint8)
        self.assert_line(self.lines(noisy, "motion_deblurring"), rf"artifacts failed \(halo_or_ringing\)\. Metric: overshoot=[\d.]+, Threshold: {self.limits.overshoot_max_sharpening}")
        self.assert_line(self.lines(noisy, "denoising"), rf"artifacts failed \(halo_or_ringing\)\. Metric: overshoot=[\d.]+, Threshold: {self.limits.overshoot_max}")

    def test_added_noise(self):
        noisy = np.clip(self.parent.astype(np.int16) + np.random.default_rng(2).normal(0, 14, self.parent.shape), 0, 255).astype(np.uint8)
        verdict = self.verdict(noisy, "motion_deblurring")
        noise = verdict["gates"]["noise"]
        self.assertEqual(noise["limit"], round(max(noise["before"] * self.limits.noise_growth_max, noise["before"] + 0.25, 0.0), 3))      # the limit it was held to, not a placeholder
        self.assertIn(f"noise failed (noise_increased). Metric: after={noise['after']}, Threshold: {noise['limit']}", refusal_lines(verdict))

    def test_a_deblur_that_did_not_sharpen(self):
        self.assert_line(self.lines(self.parent.copy(), "motion_deblurring"),
                         rf"improves_defect failed \(edges_not_restored_or_over_sharpened\)\. Metric: edge_gain=1\.0, Threshold: {self.limits.deblur_min_edge_gain} to {self.limits.deblur_max_edge_gain}")

    def test_an_enlargement_that_added_no_detail(self):
        height, width = self.parent.shape[:2]
        soft = cv2.GaussianBlur(cv2.resize(self.parent, (width * 2, height * 2), interpolation=cv2.INTER_CUBIC), (0, 0), 3.0)
        self.assert_line(self.lines(soft, "super_resolution", scale=2),
                         rf"improves_defect failed \(no_added_detail_or_invented_detail\)\. Metric: detail_ratio_vs_bicubic=[\d.]+, Threshold: 1\.0 to {self.limits.upscale_detail_gain_max}")

    def test_a_face_deblur_that_did_not_clear_the_face(self):
        x, y, w, h = 150, 100, 180, 220
        parent = base.portrait()
        parent[y:y + h, x:x + w] = cv2.GaussianBlur(parent[y:y + h, x:x + w], (0, 0), 7.0)
        report = self.analyzer.analyze(parent)
        self.assertEqual(report.face_blur.get("status"), "high")
        limits = Limits(face_min_detail_gain=1.4, face_deblur_min_detail_gain=1.2)           # two different numbers: each operation is held to, and quotes, its own
        verdict = FinalImageQualityValidator(self.analyzer, self.report, limits=limits).evaluate(parent, parent.copy(), "motion_deblurring", report)
        self.assert_line(refusal_lines(verdict), r"improves_defect failed \(face_not_clearer_or_over_sharpened\)\. Metric: face_detail_gain=[\d.]+, Threshold: 1\.2")

    def test_a_lift_that_did_not_clear_the_face(self):
        parent = base.portrait(brightness=0.12)
        report = self.analyzer.analyze(parent)
        verdict = self.validator.evaluate(parent, parent.copy(), "low_light_enhancement", report)
        self.assertIn("improves_defect failed (subject_not_clearer_or_too_bright). Metric: range_gain=0.0, Threshold: 0.15", refusal_lines(verdict))

    def test_a_denoise_that_removed_no_noise(self):
        noisy = base.portrait(noise=12.0)
        report = self.analyzer.analyze(noisy)
        verdict = self.validator.evaluate(noisy, noisy.copy(), "denoising", report)
        self.assertIn(f"improves_defect failed (noise_not_reduced_or_detail_lost). Metric: noise_reduction=0.0, Threshold: {self.limits.denoise_min_noise_reduction}", refusal_lines(verdict))

    def test_a_face_restoration_that_did_not_clear_the_face(self):
        limits = Limits(face_min_detail_gain=1.4, face_deblur_min_detail_gain=1.2)
        verdict = FinalImageQualityValidator(self.analyzer, self.report, limits=limits).evaluate(self.parent, self.parent.copy(), "face_restoration", self.report)
        self.assertIn("improves_defect failed (face_not_clearer_or_detail_invented). Metric: detail_gain=1.0, Threshold: 1.4", refusal_lines(verdict))

    def test_a_detail_layer_that_added_no_detail(self):
        soft = cv2.GaussianBlur(self.parent, (0, 0), 3.0)
        verdict = self.verdict(soft, "detail_restoration")
        self.assert_line(refusal_lines(verdict), rf"improves_defect failed \(no_added_detail_or_invented_detail\)\. Metric: structure_gain=[\d.]+, Threshold: {self.limits.detail_min_gain}")

    def test_a_colourization_that_added_no_colour(self):
        grey = cv2.cvtColor(cv2.cvtColor(self.parent, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        self.assertIn(f"improves_defect failed (no_colour_added). Metric: mean_chroma=0.0, Threshold: {self.limits.colourize_min_mean_chroma}", self.lines(grey, "colourization"))

    def test_a_lift_that_changed_the_colour_against_each_colour_limit(self):
        parent = base.portrait(brightness=0.3)
        report = self.analyzer.analyze(parent)
        swapped = parent[:, :, ::-1].copy()                                                   # hue turned
        pale = cv2.addWeighted(parent, 0.15, cv2.cvtColor(cv2.cvtColor(parent, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR), 0.85, 0)         # nearly grey
        for candidate, pattern in ((swapped, rf"colour failed \(colour_drift\)\. Metric: hue_shift=[\d.]+, Threshold: {self.limits.lift_hue_shift_max}"),
                                   (pale, rf"colour failed \(colour_washed_out\)\. Metric: saturation_ratio=[\d.]+, Threshold: {self.limits.lift_saturation_min}")):
            with self.subTest(pattern=pattern):
                self.assert_line(refusal_lines(self.validator.evaluate(parent, candidate, "low_light_enhancement", report)), pattern)

    def test_a_different_person(self):
        class Fails:
            def check(self, candidate, restoring=False):
                return {"status": "failed", "similarity": 0.31, "threshold": 0.7}

        validator = FinalImageQualityValidator(self.analyzer, self.report, identity=Fails())
        self.assertIn("identity failed (identity_changed). Metric: similarity=0.31, Threshold: 0.7", self.lines(self.parent.copy(), "motion_deblurring", validator=validator))

    def test_a_face_that_is_gone_against_the_geometry_limit(self):
        faceless = FinalImageQualityValidator(ImageQualityAnalyzer(detector=base.StubDetector([])), self.report)
        self.assertIn(f"face_geometry failed (face_lost). Metric: landmark_shift=None, Threshold: {self.limits.face_landmark_shift_max}", self.lines(self.parent.copy(), "motion_deblurring", validator=faceless))

    def test_a_gate_that_names_no_reading_lists_its_numbers_with_no_threshold(self):
        verdict = {"reasons": ["lift_shows_compression_blocks"], "gates": {"compression_blocks": {"passed": False, "status": "a stronger lift of this photograph shows the compression blocks"}}}
        self.assertEqual(refusal_lines(verdict), ["compression_blocks failed (lift_shows_compression_blocks). Metric: status=a stronger lift of this photograph shows the compression blocks, Threshold: n/a"])

    def test_the_readings_of_the_second_opinions_are_in_the_geometry_line(self):
        validator, candidate, candidate_report, parent, parent_report, _analyzer = lifted.scene(8.3, lit_dx=0.0, plain_lit_dx=1.0)
        status, reasons = lifted.judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        (line,) = refusal_lines({"reasons": reasons, "gates": {"identity": status}})
        self.assertRegex(line, r"^face_geometry failed \(face_geometry_changed\)\. Metric: landmark_shift=0\.07\d*, Threshold: 0\.06; beyond a plain enlargement of the original 0\.04\d* against 0\.035; "
                               r"from the lit image the step was given 0\.07\d*, beyond a plain enlargement of it 0\.06\d*$")

    def test_a_candidate_that_passed_has_no_lines(self):
        verdict = self.verdict(self.parent.copy(), "polish")
        self.assertEqual(refusal_lines({"reasons": [], "gates": {name: dict(gate, passed=True) for name, gate in verdict["gates"].items()}}), [])

    def test_every_gate_keeps_its_limit_next_to_its_reading_even_when_it_passes(self):
        verdict = self.verdict(self.parent.copy(), "motion_deblurring")
        for name in ("data_consistency", "highlights", "noise", "colour", "artifacts"):
            self.assertIn("limit", verdict["gates"][name], name)
            self.assertIn(verdict["gates"][name]["metric"], verdict["gates"][name], name)


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class SkippedLogTests(SimpleTestCase):
    """The engine's WARNING lines: what was skipped, for which photograph, and why."""

    def run_engine(self, runner, config=None, bias=0.0, identity=_no_identity, **settings_):
        source = cv2.resize(base.portrait(), (320, 240))
        source[:40] = 246
        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=BiasedDetector(guard.FACE, bias, 320)), config=config or EngineConfig(), runners={ops.UPSCALE: runner})
        with override_settings(**settings_), patch("enhancement.adaptive.service.IdentityChecker", identity):
            service.enhance(source)

    def logged(self, level, runner, **options):
        with self.assertLogs(LOGGER, level=level) as logged:
            self.run_engine(runner, **options)
        return [record.getMessage() for record in logged.records if record.getMessage().startswith("SKIPPED ")]

    def test_a_refused_enlargement_names_the_model_the_photograph_the_gate_the_reading_and_the_limit(self):
        with photo("183 (result 9)"):
            lines = self.logged("WARNING", guard.EngineTests.brightening_enlargement, ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^SKIPPED realesrgan for Photo ID 183 \(result 9\)\. Reason: highlights failed \(highlight_clipping_increased\)\. Metric: clipping_growth=[\d.]+, Threshold: 3\.0$")

    def test_the_model_is_named_as_the_candidate_names_it(self):
        def other_name(image, engine, variant):
            enlarged, _models = clear_enlargement(image, engine, variant)
            return (enlarged.astype("int16") + 8 * (enlarged > 200)).clip(0, 255).astype("uint8"), [{"model": "realesrgan_x4plus", "device": "stand-in"}]

        lines = self.logged("WARNING", other_name, ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False)
        self.assertTrue(lines[0].startswith("SKIPPED realesrgan_x4plus for Photo ID n/a."), lines)

    def test_a_refused_retry_is_logged_too(self):
        real, calls = FinalImageQualityValidator.evaluate, []

        def scripted(validator, parent, candidate, operation, parent_report, *args, **kwargs):
            verdict = real(validator, parent, candidate, operation, parent_report, *args, **kwargs)
            if operation == ops.UPSCALE:
                calls.append(1)
                if len(calls) == 2:                                          # the highlight guard's second candidate fails another gate
                    verdict["accepted"] = False
                    verdict["gates"]["artifacts"] = {"passed": False, "overshoot": 0.5, "metric": "overshoot", "limit": 0.04, "reason": "halo_or_ringing"}
                    verdict["reasons"] = ["halo_or_ringing"]
            return verdict

        with patch.object(FinalImageQualityValidator, "evaluate", scripted):
            lines = self.logged("WARNING", guard.EngineTests.brightening_enlargement)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("highlights failed", lines[0])
        self.assertEqual(lines[1], "SKIPPED realesrgan for Photo ID n/a. Reason: artifacts failed (halo_or_ringing). Metric: overshoot=0.5, Threshold: 0.04")

    def test_without_a_task_the_photograph_is_n_a(self):
        lines = self.logged("WARNING", guard.EngineTests.brightening_enlargement, ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False)
        self.assertTrue(lines[0].startswith("SKIPPED realesrgan for Photo ID n/a. Reason: highlights failed"), lines)

    def test_a_refusal_for_the_face_geometry_gives_the_shift_and_the_limit(self):
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0):
            lines = self.logged("WARNING", clear_enlargement, bias=0.08)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^SKIPPED realesrgan for Photo ID n/a\. Reason: face_geometry failed \(face_geometry_changed\)\. Metric: landmark_shift=0\.0[78]\d*, Threshold: 0\.06$")

    def test_the_guards_retry_that_passes_is_not_a_skip_but_the_first_candidate_is(self):
        lines = self.logged("WARNING", guard.EngineTests.brightening_enlargement)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("highlights failed", lines[0])

    def test_a_model_that_cannot_run_is_logged_with_the_reason(self):
        def unavailable(image, engine, variant):
            raise HDRestorationUnavailable("The Real-ESRGAN checkpoint is unavailable.")

        lines = self.logged("WARNING", unavailable)
        self.assertEqual(lines, ["SKIPPED realesrgan for Photo ID n/a. Reason: model unavailable (The Real-ESRGAN checkpoint is unavailable.)"])

    def test_an_operation_the_router_left_out_is_logged_once_with_its_reason(self):
        lines = self.logged("INFO", clear_enlargement, config=EngineConfig(max_output_edge=400))
        self.assertEqual(lines, ["SKIPPED super_resolution for Photo ID n/a. Reason: Upscaling would exceed the output size limit."])

    def test_a_step_with_no_engine_is_logged(self):
        decisions = iter([Decision(step=Step(ops.UPSCALE, [], [{"name": "x2", "scale": 2}], "The photograph is small."), skipped=[])])
        with patch("enhancement.adaptive.router.AdaptiveEnhancementRouter.next_step", side_effect=lambda *a, **k: next(decisions, Decision(step=None))):
            lines = self.logged("WARNING", clear_enlargement)
        self.assertEqual(lines, ["SKIPPED super_resolution for Photo ID n/a. Reason: no engine for this operation is switched on"])

    def test_a_face_restoration_without_a_verifiable_identity_is_logged(self):
        blurry = base.portrait(face_box=(150, 100, 180, 220))
        x, y, w, h = 150, 100, 180, 220
        blurry[y:y + h, x:x + w] = cv2.GaussianBlur(blurry[y:y + h, x:x + w], (0, 0), 6.0)
        service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(), config=EngineConfig(), runners={ops.FACE: lambda image, engine, variant: (image, [{"model": engine}])})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity), self.assertLogs(LOGGER, level="WARNING") as logged:
            service.enhance(blurry)
        skipped = [record.getMessage() for record in logged.records if record.getMessage().startswith("SKIPPED face_restoration ")]       # (the deblur's model may be absent where this runs)
        self.assertEqual(len(skipped), 1, skipped)
        self.assertRegex(skipped[0], r"^SKIPPED face_restoration for Photo ID n/a\. Reason: Identity could not be verified, so no generative face restoration is applied\. ")

    def test_an_accepted_enlargement_logs_no_skip(self):
        with self.assertNoLogs(LOGGER, level="WARNING"):
            self.run_engine(clear_enlargement)


@override_settings(**NO_MODELS)
class TaskLabelTests(ResultFixture):
    def test_the_task_labels_the_engine_with_the_photograph_and_its_result(self):
        result = self.make_result()
        seen = []

        def run(*args, **kwargs):
            seen.append(PHOTO.get())
            return enhance_bgr(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=run):
            process_enhancement_image.apply(args=[result.pk]).get()
        self.assertEqual(seen, [f"{result.image_id} (result {result.pk})"])
        self.assertEqual(PHOTO.get(), "n/a")                                   # not left behind for the next job of the worker

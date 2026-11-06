"""An enlargement refused ONLY for its highlights gets one more judgement, with the brightening the model added to bright pixels taken back (ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD).

Found on the library's 25.jpg (a man in shadow in front of a window): after the whole-frame lift the sky sat at the top of the range, Real-ESRGAN added +0.9 levels to it, and the validator
counted 8.0 points of newly clipped pixels (the limit is 3.0), so the lifted photograph kept its size although the face geometry and identity gates had passed
(docs/evaluation/2026-11-06-enlargement-after-lift.md). The models are stand-ins here: what is tested is the rule, the weights, and that nothing which works without the guard changes.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.candidates import CandidatePool
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator
from .test_adaptive_enlargement_geometry import BiasedDetector, clear_enlargement
from .test_adaptive_polish import _no_identity

FACE = (75, 50, 90, 110)                 # the face of the 320x240 photograph below (the engine tests' portrait, halved)


def flat(value, shape=(40, 60)):
    return np.full((*shape, 3), value, np.uint8)


def clipped_percent(image):
    """What the validator's highlights gate counts: the share of pixels at luminance 250 and above."""
    return float((cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) >= 250).mean() * 100)


class SettingTests(SimpleTestCase):
    def test_it_is_on_by_default(self):
        self.assertIs(settings.ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD, True)


class KeepHighlightsTests(SimpleTestCase):
    def test_a_bright_pixel_the_model_made_brighter_is_taken_back(self):
        np.testing.assert_array_equal(ops.keep_highlights(flat(255), flat(246)), flat(246))

    def test_a_mid_tone_keeps_what_the_model_added(self):
        np.testing.assert_array_equal(ops.keep_highlights(flat(140), flat(120)), flat(140))

    def test_a_face_in_the_shadows_keeps_what_the_model_added(self):
        np.testing.assert_array_equal(ops.keep_highlights(flat(70), flat(30)), flat(70))

    def test_a_bright_pixel_the_model_made_darker_stays_darker(self):
        np.testing.assert_array_equal(ops.keep_highlights(flat(236), flat(246)), flat(236))

    def test_the_cap_starts_at_the_low_edge_of_the_ramp_and_is_complete_at_its_top(self):
        for value, expected in ((ops.HIGHLIGHT_KEEP_FROM, 10), (ops.HIGHLIGHT_KEEP_FULL, 0)):
            with self.subTest(reference=value):
                reference = flat(int(value))
                out = ops.keep_highlights(np.clip(reference.astype(np.int16) + 10, 0, 255).astype(np.uint8), reference)
                self.assertEqual(int((out.astype(np.int16) - reference).max()), expected)

    def test_the_cap_fades_in_without_a_step(self):
        ramp = np.repeat(np.arange(200, 256, dtype=np.uint8)[None, :, None], 3, axis=2)        # one row of luminance 200 .. 255
        candidate = np.clip(ramp.astype(np.int16) + 10, 0, 255).astype(np.uint8)
        excess = (ops.keep_highlights(candidate, ramp).astype(np.int16) - ramp)[0, :, 0]
        self.assertEqual(int(excess[0]), 10)                                                    # under the ramp: the candidate's own
        self.assertEqual(int(excess[-1]), 0)                                                    # above it: not brighter than the reference
        self.assertTrue((np.diff(excess) <= 0).all(), excess)                                   # never brighter again further up
        self.assertLessEqual(int(np.abs(np.diff(excess)).max()), 1, excess)                     # no seam

    def test_a_pixel_inside_the_ramp_is_a_rounded_mix_of_the_two(self):
        """Reference 225, candidate 240: the weight is smoothstep(0.4) = 0.352, so 240 x 0.648 + 225 x 0.352 = 234.72, which is 235."""
        np.testing.assert_array_equal(ops.keep_highlights(flat(240), flat(225)), flat(235))

    def test_every_channel_is_capped_on_its_own(self):
        reference = np.full((4, 4, 3), 250, np.uint8)
        candidate = np.empty((4, 4, 3), np.uint8)
        candidate[:] = (255, 250, 252)
        np.testing.assert_array_equal(ops.keep_highlights(candidate, reference), reference)

    def test_the_result_is_never_brighter_than_the_candidate(self):
        rng = np.random.default_rng(4)
        reference = rng.integers(0, 256, (50, 70, 3), dtype=np.uint8)
        candidate = np.clip(reference.astype(np.int16) + rng.integers(-20, 25, reference.shape), 0, 255).astype(np.uint8)
        out = ops.keep_highlights(candidate, reference)
        self.assertTrue((out <= candidate).all())

    def test_an_identical_candidate_is_returned_as_it_is(self):
        rng = np.random.default_rng(5)
        reference = rng.integers(0, 256, (30, 30, 3), dtype=np.uint8)
        np.testing.assert_array_equal(ops.keep_highlights(reference.copy(), reference), reference)

    def test_a_scene_with_a_bright_sky_and_a_dark_subject_changes_only_the_sky(self):
        reference = flat(100, (60, 80))
        reference[:20] = 246
        candidate = np.clip(reference.astype(np.int16) + 8, 0, 255).astype(np.uint8)
        out = ops.keep_highlights(candidate, reference)
        np.testing.assert_array_equal(out[:20], reference[:20])
        np.testing.assert_array_equal(out[20:], candidate[20:])

    def test_a_new_uint8_array_of_the_same_shape_and_the_arguments_are_untouched(self):
        reference, candidate = flat(246, (10, 12)), flat(255, (10, 12))
        before = (reference.copy(), candidate.copy())
        out = ops.keep_highlights(candidate, reference)
        self.assertEqual((out.dtype, out.shape), (np.uint8, candidate.shape))
        self.assertIsNot(out, candidate)
        np.testing.assert_array_equal(reference, before[0])
        np.testing.assert_array_equal(candidate, before[1])


def refused(reasons, accepted=False):
    return {"accepted": accepted, "reasons": list(reasons), "gates": {}}


class RetryTests(SimpleTestCase):
    """``AdaptiveImageEnhancementService._retry_with_highlights_kept``: when it applies, what it makes and judges, and that it leaves everything else alone."""

    def setUp(self):
        self.parent = np.full((30, 40, 3), 246, np.uint8)
        self.pool = CandidatePool(self.parent)
        self.current = self.pool.original
        self.candidate = SimpleNamespace(image=np.full((60, 80, 3), 255, np.uint8), variant="x2", models=[{"model": "realesrgan"}], verdict=refused(["highlight_clipping_increased"]))
        self.step = SimpleNamespace(operation=ops.UPSCALE)
        self.validator = Mock()
        self.validator.evaluate.return_value = refused([], accepted=True)

    def retry(self, **changes):
        for name, value in changes.items():
            setattr(self.candidate, name, value)
        return AdaptiveImageEnhancementService._retry_with_highlights_kept(self.step, self.candidate, self.current, "report", self.pool, self.validator, 2)

    def test_a_refusal_for_the_highlights_alone_makes_a_second_candidate_and_judges_it(self):
        kept = self.retry()
        self.assertIsNotNone(kept)
        np.testing.assert_array_equal(kept.image, np.full((60, 80, 3), 246, np.uint8))              # the model's brightening is gone: the plain enlargement's level
        self.assertEqual(kept.variant, "x2, highlights kept")
        self.assertEqual(kept.models, [{"model": "realesrgan"}])                                    # the same model made it: the record says so
        self.assertEqual(kept.parent, self.current.id)
        self.assertEqual(kept.verdict, refused([], accepted=True))
        self.assertIn(kept, self.pool.items)
        (parent, image, operation, report), options = self.validator.evaluate.call_args
        self.assertIs(parent, self.parent)
        self.assertIs(image, kept.image)
        self.assertEqual((operation, report, options), (ops.UPSCALE, "report", {"expected_scale": 2}))      # the same gates, against the same parent, at the same scale

    def test_the_retry_is_judged_not_trusted(self):
        self.validator.evaluate.return_value = refused(["halo_or_ringing"])
        kept = self.retry()
        self.assertFalse(kept.verdict["accepted"])
        self.assertEqual(kept.verdict["reasons"], ["halo_or_ringing"])

    def test_an_accepted_enlargement_is_left_alone(self):
        self.assertIsNone(self.retry(verdict=refused([], accepted=True)))
        self.validator.evaluate.assert_not_called()
        self.assertEqual(len(self.pool.items), 1)

    def test_a_refusal_for_anything_else_as_well_is_left_alone(self):
        for reasons in (["face_geometry_changed", "highlight_clipping_increased"], ["highlight_clipping_increased", "identity_changed"], ["face_geometry_changed"], ["noise_increased"], []):
            with self.subTest(reasons=reasons):
                self.assertIsNone(self.retry(verdict=refused(reasons)))
        self.validator.evaluate.assert_not_called()
        self.assertEqual(len(self.pool.items), 1)

    def test_only_an_enlargement_is_retried(self):
        for operation in (ops.DETAIL, ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR, ops.FACE, ops.POLISH):
            with self.subTest(operation=operation):
                self.step = SimpleNamespace(operation=operation)
                self.assertIsNone(self.retry())
        self.validator.evaluate.assert_not_called()

    def test_the_setting_switches_it_off(self):
        with override_settings(ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False):
            self.assertIsNone(self.retry())
        self.validator.evaluate.assert_not_called()
        self.assertEqual(len(self.pool.items), 1)

    def test_a_full_pool_is_not_an_error(self):
        pool = CandidatePool(self.parent, max_candidates=2)
        pool.add(np.zeros((2, 2, 3), np.uint8), pool.original, "denoising")                        # two images held: the limit
        self.pool = pool
        self.assertIsNone(self.retry())
        self.validator.evaluate.assert_not_called()


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class EngineTests(SimpleTestCase):
    """The whole engine on a small photograph with a bright sky (a window): the enlargement stand-in either leaves the sky as it is or adds six levels to it."""

    def source(self):
        image = cv2.resize(base.portrait(), (320, 240))
        image[:40] = 246                                                       # a bright band: one more level and it is "clipped" (luminance 250 and above) after a few more
        return image

    @staticmethod
    def brightening_enlargement(image, engine, variant):
        """What Real-ESRGAN did to the sky of the library's 25.jpg: a few levels more on every bright pixel."""
        enlarged, models = clear_enlargement(image, engine, variant)
        bright = (cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY) > 200)[..., None]
        return np.where(bright, np.clip(enlarged.astype(np.int16) + 6, 0, 255), enlarged).astype(np.uint8), models

    def run_engine(self, runner, bias=0.0):
        source = self.source()
        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=BiasedDetector(FACE, bias, 320)), config=EngineConfig(), runners={ops.UPSCALE: runner})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
            output, _level, parameters = service.enhance(source)
        stage = next((s for s in parameters["adaptive"]["stages"] if s["operation"] == ops.UPSCALE), None)
        return source, output, stage, parameters

    def test_the_premise_without_the_guard_the_brightened_sky_alone_refuses_the_enlargement(self):
        with override_settings(ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False):
            source, output, stage, _ = self.run_engine(self.brightening_enlargement)
        self.assertEqual(stage["status"], "rejected")
        self.assertEqual(stage["rejection_reasons"], ["highlight_clipping_increased"])
        self.assertEqual(output.shape, source.shape)

    def test_with_the_guard_the_enlargement_is_made(self):
        source, output, stage, parameters = self.run_engine(self.brightening_enlargement)
        self.assertEqual(stage["status"], "accepted", stage.get("rejection_reasons"))
        self.assertIn("highlights kept", stage["variant"])
        self.assertEqual(output.shape[:2], (480, 640))
        self.assertIn(ops.UPSCALE, parameters["adaptive"]["operations_applied"])
        self.assertTrue(any("highlights alone" in note for note in stage["notes"]), stage["notes"])

    def test_the_sky_of_the_result_is_not_brighter_than_a_plain_enlargement_of_it(self):
        source, output, _stage, _ = self.run_engine(self.brightening_enlargement)
        plain = cv2.resize(source, (640, 480), interpolation=cv2.INTER_CUBIC)
        self.assertLessEqual(int(output[:70].max()), int(plain[:70].max()))
        self.assertLess(clipped_percent(output), 1.0)

    def test_the_model_runs_once_the_retry_costs_no_second_run(self):
        runs = []

        def counting(image, engine, variant):
            runs.append(variant["name"])
            return self.brightening_enlargement(image, engine, variant)

        self.run_engine(counting)
        self.assertEqual(runs, ["x2"])

    def test_an_enlargement_that_passes_without_the_guard_comes_out_identical_with_it(self):
        with override_settings(ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False):
            _s, off, stage_off, parameters_off = self.run_engine(clear_enlargement)
        _s, on, stage_on, parameters_on = self.run_engine(clear_enlargement)
        self.assertEqual(stage_off["status"], "accepted", stage_off.get("rejection_reasons"))
        np.testing.assert_array_equal(on, off)
        self.assertEqual(stage_on["candidates"], stage_off["candidates"])                          # no second candidate was made
        self.assertEqual(stage_on["variant"], stage_off["variant"])
        self.assertEqual([c["variant"] for c in parameters_on["adaptive"]["candidates"]], [c["variant"] for c in parameters_off["adaptive"]["candidates"]])
        self.assertFalse(any("highlights alone" in note for note in stage_on["notes"]))

    def test_a_refusal_for_the_highlights_and_the_face_geometry_is_not_retried(self):
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0):                         # the detector's placement of the enlarged face is refused, as before the geometry rule
            source, output, stage, parameters = self.run_engine(self.brightening_enlargement, bias=0.08)
        self.assertEqual(stage["status"], "rejected")
        self.assertEqual(stage["rejection_reasons"], ["face_geometry_changed", "highlight_clipping_increased"])
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(any("highlights kept" in c["variant"] for c in parameters["adaptive"]["candidates"]))

    def test_the_retry_has_to_pass_every_gate_too(self):
        """The validator refuses the retry for another reason: the enlargement is not made, and both refusals are recorded."""
        real = FinalImageQualityValidator.evaluate
        calls = []

        def scripted(validator, parent, candidate, operation, parent_report, *args, **kwargs):
            verdict = real(validator, parent, candidate, operation, parent_report, *args, **kwargs)
            if operation == ops.UPSCALE:
                calls.append(candidate.shape[:2])
                if len(calls) == 2:
                    verdict["accepted"], verdict["reasons"] = False, ["halo_or_ringing"]
            return verdict

        added, real_add = [], CandidatePool.add

        def recording_add(pool, *args, **kwargs):
            candidate = real_add(pool, *args, **kwargs)
            added.append(candidate)
            return candidate

        with patch.object(FinalImageQualityValidator, "evaluate", scripted), patch.object(CandidatePool, "add", recording_add):
            source, output, stage, _ = self.run_engine(self.brightening_enlargement)
        self.assertEqual([c.image for c in added if "highlights kept" in c.variant], [None])           # the refused retry's pixels are freed like any refused candidate's
        self.assertEqual(len(calls), 2)
        self.assertEqual(stage["status"], "rejected")
        self.assertEqual(stage["rejection_reasons"], ["halo_or_ringing", "highlight_clipping_increased"])
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(any("highlights alone" in note for note in stage["notes"]), stage["notes"])           # the note says the enlargement passed: it did not

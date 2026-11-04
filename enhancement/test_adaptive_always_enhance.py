"""Every photograph is enhanced (ADAPTIVE_ALWAYS_ENHANCE): a photograph on which no defect was found goes through the detail model used for light photographs
(the compact "general" network) as a refinement, and through a finishing polish when that model's result is refused; a refused corrective step no longer keeps
the photograph from the finish; and a refused candidate no longer uses up the candidate pool the finish needs.

As elsewhere in the adaptive tests, models are replaced by stand-ins: these are tests of decisions and of what is measured. The real-photograph evidence is
in docs/evaluation/2026-11-03-always-enhance.md.
"""
import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_polish as polish_tests
from .adaptive import operations as ops
from .adaptive.candidates import CandidatePool, PoolFull
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import ORIGINAL_PRESERVED, AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait, runner_lift

ON = dict(ALL_ON, ADAPTIVE_DETAIL_ENABLED=True, ADAPTIVE_ALWAYS_ENHANCE=True)


def mild_sharpen(image, amount=0.6):
    """A stand-in for the detail model: a little real structure added, nothing invented."""
    blurred = cv2.GaussianBlur(image, (0, 0), 1.5)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


def runner_detail_adds_structure(image, engine, variant):
    return mild_sharpen(image), [{"model": engine, "device": "stand-in"}]


def runner_detail_changes_nothing(image, engine, variant):
    return image.copy(), [{"model": engine, "device": "stand-in"}]


def run(image, runners=None, style="natural", identity=True, analyzer=None, **settings_):
    from unittest.mock import patch

    service = AdaptiveImageEnhancementService(analyzer=analyzer or polish_tests.skin_analyzer(), config=EngineConfig(), runners=runners or {})
    checker = polish_tests.FakeIdentity if identity else polish_tests._no_identity
    with override_settings(**{**ON, **settings_}), patch("enhancement.adaptive.service.IdentityChecker", checker):
        return service.enhance(image, style=style)


class ChangedIdentity(polish_tests.FakeIdentity):
    """An identity check that says every candidate is somebody else."""

    def check(self, candidate):
        return {"status": "failed", "similarity": 0.2, "threshold": self.threshold}


class SettingsAndProfileTests(SimpleTestCase):
    def test_the_defaults(self):
        self.assertIs(settings.ADAPTIVE_ALWAYS_ENHANCE, True)
        self.assertEqual((settings.ADAPTIVE_FINISH_STRENGTH, settings.ADAPTIVE_FINISH_VIBRANCE, settings.ADAPTIVE_FINISH_TOLERANCE, settings.ADAPTIVE_FINISH_MIN_CHANGE), (3.0, 0.15, 0.97, 1.5))
        self.assertEqual((settings.ADAPTIVE_REFINE_MIN_GAIN, settings.ADAPTIVE_REFINE_MIN_CHANGE), (0.75, 0.5))

    @override_settings(ADAPTIVE_ALWAYS_ENHANCE=True, ADAPTIVE_FINISH_STRENGTH=3.0)
    def test_the_finish_is_stronger_and_may_touch_a_dark_scene(self):
        self.assertEqual((profile_for("natural").polish_scale, profile_for("natural").polish_dark_scenes), (3.0, True))
        self.assertEqual(profile_for("ai_max").polish_scale, 3.75)
        self.assertEqual(profile_for("natural", "low").polish_scale, 1.8)

    @override_settings(ADAPTIVE_ALWAYS_ENHANCE=False)
    def test_with_it_off_the_profile_is_what_it_was(self):
        self.assertEqual((profile_for("natural").polish_scale, profile_for("natural").polish_dark_scenes), (1.0, False))


@override_settings(**ON)
class RouterTests(SimpleTestCase):
    def router(self):
        return AdaptiveEnhancementRouter(EngineConfig())

    def report(self, image=None):
        return analyzer_with().analyze(image if image is not None else portrait())

    def test_the_final_layer_is_the_light_photo_model_for_every_photograph_whatever_its_condition(self):
        for image in (portrait(), portrait(brightness=0.12), portrait(noise=25.0)):          # clean, dark and noisy
            step = self.router().final_layer_step(self.report(image))
            self.assertEqual((step.operation, step.engines), (ops.DETAIL, ["general"]))        # the compact model only: the slow networks are not its fallback
            self.assertTrue(step.optional)
            self.assertTrue(step.reason.startswith("Final layer"))
            self.assertTrue(all(variant["refine"] for variant in step.variants))

    def test_the_router_itself_plans_no_final_layer_it_is_the_service_that_asks_for_it_last(self):
        step = self.router().next_step(self.report(), set(), polish=None, profile=profile_for("natural")).step
        self.assertIsNone(step)

    def test_the_finishing_polish_is_the_fallback_with_its_ladder(self):
        step = self.router().finish_step(self.report(), profile_for("natural"))
        self.assertEqual(step.operation, ops.POLISH)
        self.assertEqual([variant["name"] for variant in step.variants], ["finish", "finish blend 0.65", "finish soft edges", "finish gentle", "finish colour"])
        self.assertTrue(all(variant["vibrance"] == 0.15 for variant in step.variants))
        self.assertEqual((step.variants[0]["clahe"], step.variants[0]["amount"]), (0.75, 0.7))        # 0.25 and 0.25 times 3, the amount capped at 0.7
        self.assertEqual(step.variants[-1]["amount"], 0.0)                # the last resort sharpens nothing: no halo, no over-sharpened face

    def test_a_photograph_with_a_defect_is_still_corrected_first(self):
        report = self.report(portrait(brightness=0.12))
        self.assertEqual(self.router().next_step(report, set(), polish=None, profile=profile_for("natural")).step.operation, ops.LOW_LIGHT)

    def test_without_the_compact_model_there_is_no_final_layer_and_the_polish_is_the_fallback(self):
        with override_settings(HD_RESTORATION_ENABLED=False):
            self.assertIsNone(self.router().final_layer_step(self.report()))
            self.assertEqual(self.router().finish_step(self.report(), profile_for("natural")).operation, ops.POLISH)

    def test_a_photograph_over_the_pixel_limit_skips_the_model(self):
        with override_settings(ADAPTIVE_DETAIL_MAX_PIXELS=1000):
            self.assertIsNone(self.router().final_layer_step(self.report()))

    @override_settings(ADAPTIVE_ALWAYS_ENHANCE=False)
    def test_with_it_off_there_is_no_final_layer_no_finish_and_the_polish_has_two_variants(self):
        router = self.router()
        self.assertIsNone(router.final_layer_step(self.report()))
        self.assertIsNone(router.finish_step(self.report(), profile_for("natural")))
        step = router.next_step(self.report(), set(), polish={"style": "natural"}, profile=profile_for("natural")).step
        self.assertEqual((step.operation, [variant["name"] for variant in step.variants]), (ops.POLISH, ["blend 1.0", "blend 0.65"]))


@override_settings(**ON)
class EngineTests(SimpleTestCase):
    def source(self):
        return polish_tests.soft_portrait()

    def test_the_light_photo_model_enhances_a_photograph_with_no_defect(self):
        source = self.source()
        out, _level, parameters = run(source, runners={ops.DETAIL: runner_detail_adds_structure})
        adaptive = parameters["adaptive"]
        self.assertEqual(adaptive["operations_applied"], [ops.DETAIL])
        self.assertEqual(adaptive["outcome"], "enhanced")
        self.assertEqual(adaptive["summary"], "Enhanced: refined the fine detail with the enhancement model.")
        self.assertFalse(np.array_equal(out, source))
        self.assertNotIn(ops.POLISH, [stage["operation"] for stage in adaptive["stages"]])          # nothing is stacked on an accepted step

    def test_when_the_model_adds_nothing_the_finishing_polish_enhances_it(self):
        source = self.source()
        out, _level, parameters = run(source, runners={ops.DETAIL: runner_detail_changes_nothing})
        adaptive = parameters["adaptive"]
        stages = {stage["operation"]: stage["status"] for stage in adaptive["stages"]}
        self.assertEqual(stages, {ops.DETAIL: "rejected", ops.POLISH: "accepted"})
        self.assertEqual(adaptive["operations_applied"], [ops.POLISH])
        self.assertEqual(adaptive["outcome"], "enhanced")
        self.assertFalse(np.array_equal(out, source))
        self.assertGreaterEqual(float(np.abs(out.astype(np.int16) - source.astype(np.int16)).mean()), 1.5)          # a visible change, not a no-op

    def test_the_final_layer_is_stacked_on_a_correction(self):
        """A dark photograph is lifted, and the light-photo model is then applied on top of the lift, whatever the lift needed: the last layer of every photograph."""
        dark = portrait(brightness=0.12)
        gentle = lambda image, engine, variant: (mild_sharpen(image, 0.15), [{"model": engine, "device": "stand-in"}])
        _out, _level, parameters = run(dark, runners={ops.LOW_LIGHT: runner_lift, ops.DETAIL: gentle}, analyzer=analyzer_with(), identity=False)
        adaptive = parameters["adaptive"]
        self.assertEqual(adaptive["operations_applied"], [ops.LOW_LIGHT, ops.DETAIL])
        self.assertEqual(adaptive["summary"].split(": ")[1].count("refined the fine detail with the enhancement model"), 1)
        self.assertIn("brightened", adaptive["summary"])

    def test_what_is_left_unresolved_is_judged_before_the_final_layer(self):
        """The model's smoothing can read as a blur to the analyzer; a defect it did not touch is reported as it was before, not as the model left it."""
        source = self.source()
        _out, _level, parameters = run(source, runners={ops.DETAIL: runner_detail_adds_structure})
        self.assertEqual(parameters["adaptive"]["outcome"], "enhanced")
        self.assertEqual(parameters["adaptive"]["unresolved_defects"], [])

    def test_a_photograph_over_the_pixel_limit_is_finished_not_refined(self):
        _out, _level, parameters = run(self.source(), runners={ops.DETAIL: runner_detail_adds_structure}, ADAPTIVE_DETAIL_MAX_PIXELS=1000)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.POLISH])

    def test_it_does_not_need_the_polish_switch(self):
        _out, _level, parameters = run(self.source(), runners={ops.DETAIL: runner_detail_changes_nothing}, ADAPTIVE_POLISH_ENABLED=False)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.POLISH])

    @override_settings(ADAPTIVE_ALWAYS_ENHANCE=False)
    def test_with_it_off_the_same_photograph_is_returned_as_it_was(self):
        source = self.source()
        out, _level, parameters = run(source, ADAPTIVE_ALWAYS_ENHANCE=False, ADAPTIVE_POLISH_ENABLED=False)
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        np.testing.assert_array_equal(out, source)

    def test_a_refused_correction_no_longer_keeps_the_photograph_from_the_finish(self):
        """A lift that the gates refuse leaves the photograph as it was; the finishing polish still gets its turn (it used to be skipped)."""
        dark = portrait(brightness=0.2)
        too_bright = lambda image, engine, variant: (np.full_like(image, 250), [{"model": engine}])               # refused by every gate
        _out, _level, parameters = run(dark, runners={ops.LOW_LIGHT: too_bright}, identity=False)
        stages = {stage["operation"]: stage["status"] for stage in parameters["adaptive"]["stages"]}
        self.assertEqual(stages[ops.LOW_LIGHT], "rejected")
        self.assertIn(ops.POLISH, stages)

    def test_the_identity_gates_still_win(self):
        """A finish that would change who the face is must be refused: the identity stand-in only passes the unchanged face."""
        source = self.source()
        from unittest.mock import patch

        service = AdaptiveImageEnhancementService(analyzer=polish_tests.skin_analyzer(), config=EngineConfig(), runners={ops.DETAIL: runner_detail_adds_structure})
        with override_settings(**ON), patch("enhancement.adaptive.service.IdentityChecker", lambda original, allowed=True: ChangedIdentity(original)):
            _out, _level, parameters = service.enhance(source, style="natural")
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)


@override_settings(**ON)
class ValidatorTests(SimpleTestCase):
    def setUp(self):
        self.source = polish_tests.soft_portrait()
        self.analyzer = polish_tests.skin_analyzer()
        self.report = self.analyzer.analyze(self.source)
        self.validator = FinalImageQualityValidator(self.analyzer, self.report, None)

    def test_a_refinement_that_adds_real_structure_passes_without_a_defect_to_resolve(self):
        verdict = self.validator.evaluate(self.source, mild_sharpen(self.source), "detail_restoration", self.report, refine=True)
        self.assertTrue(verdict["gates"]["improves_defect"]["passed"], verdict["gates"]["improves_defect"])

    def test_the_same_candidate_is_refused_when_it_is_judged_as_a_defect_fix(self):
        clean = portrait()
        report = self.analyzer.analyze(clean)
        self.assertEqual((report.blur["status"], report.compression["status"]), ("none", "none"))
        validator = FinalImageQualityValidator(self.analyzer, report, None)
        candidate = clean.copy()                                                    # structure is added around the face, not to it (a sharp face is already above the ceiling)
        x, y, w, h = polish_tests.FACE
        sharpened = mild_sharpen(clean, 0.3)
        candidate[:y] = sharpened[:y]
        candidate[y + h:] = sharpened[y + h:]
        verdict = validator.evaluate(clean, candidate, "detail_restoration", report, refine=False)
        self.assertFalse(verdict["gates"]["improves_defect"]["passed"])           # no blur or compression got better: nothing to resolve
        refined = validator.evaluate(clean, candidate, "detail_restoration", report, refine=True)
        self.assertTrue(refined["gates"]["improves_defect"]["passed"], refined["gates"]["improves_defect"])

    def test_a_refinement_that_adds_nothing_is_refused(self):
        verdict = self.validator.evaluate(self.source, self.source.copy(), "detail_restoration", self.report, refine=True)
        self.assertEqual(verdict["reasons"].count("no_added_detail_or_invented_detail"), 1)

    def test_a_model_that_cleans_noise_is_not_refused_for_losing_structure(self):
        """On a brightened dark photograph the model removes amplified noise, which the structure energy reads as a loss of 0.6-0.9; the floor allows it, a heavy smoothing is still refused."""
        rng = np.random.default_rng(3)
        noisy = np.clip(self.source.astype(np.float32) + rng.normal(0, 6, self.source.shape), 0, 255).astype(np.uint8)
        report = self.analyzer.analyze(noisy)
        validator = FinalImageQualityValidator(self.analyzer, report, None)
        cleaned = cv2.addWeighted(noisy, 0.5, cv2.GaussianBlur(noisy, (0, 0), 1.0), 0.5, 0)
        gate = validator.evaluate(noisy, cleaned, "detail_restoration", report, refine=True)["gates"]["improves_defect"]
        self.assertLess(gate["structure_gain"], 1.0)
        self.assertGreaterEqual(gate["structure_gain"], 0.75)
        self.assertGreaterEqual(gate["mean_change"], 0.5)
        self.assertTrue(gate["passed"], gate)
        smoothed = cv2.GaussianBlur(noisy, (0, 0), 3.0)
        self.assertFalse(validator.evaluate(noisy, smoothed, "detail_restoration", report, refine=True)["gates"]["improves_defect"]["passed"])

    def test_an_invented_amount_of_structure_is_still_refused(self):
        verdict = self.validator.evaluate(self.source, mild_sharpen(self.source, 6.0), "detail_restoration", self.report, refine=True)
        self.assertFalse(verdict["accepted"])


class CandidatePoolTests(SimpleTestCase):
    def pool(self, limit=4):
        original = np.zeros((8, 8, 3), np.uint8)
        return CandidatePool(original, limit), original

    def test_a_refused_candidate_is_released_and_no_longer_counts_against_the_limit(self):
        pool, original = self.pool(limit=3)
        first = pool.add(original.copy(), pool.original, "low_light_enhancement")
        second = pool.add(original.copy(), pool.original, "low_light_enhancement")
        with self.assertRaises(PoolFull):
            pool.add(original.copy(), pool.original, "polish")
        first.release()
        second.release()
        third = pool.add(original.copy(), pool.original, "polish")          # room again, as the later operations need
        self.assertEqual(third.id, "c3_polish")                              # ids stay unique
        self.assertIsNone(first.image)

    def test_the_total_ever_proposed_is_still_capped(self):
        pool, original = self.pool(limit=2)
        with self.assertRaises(PoolFull):
            for _ in range(2 * CandidatePool.TOTAL_FACTOR + 5):
                pool.add(original.copy(), pool.original, "polish").release()          # released at once, so only the total cap can stop it

    def test_a_released_candidate_still_describes_itself_and_the_original_is_never_released(self):
        pool, original = self.pool()
        candidate = pool.add(np.zeros((6, 10, 3), np.uint8), pool.original, "polish")
        candidate.release()
        described = pool.describe()
        self.assertEqual(described[1]["dimensions"], [10, 6])
        self.assertEqual(described[0]["dimensions"], [8, 8])
        self.assertIsNotNone(pool.original.image)


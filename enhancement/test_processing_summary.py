"""`processing_summary`: which engine produced each stage of a stored result, and whether a classical OpenCV engine stood in for a learned model.

It is read from the stored parameters only (enhancement/restoration.py), so most tests here hand it the records the engine writes. The consistency tests at the end run the REAL adaptive
engine with stand-in models and check the summary against what the engine itself recorded as the chosen model (the `algorithm` string), so a change to how the engine walks its
engines, or to the names of its fallback steps, fails here instead of silently misreporting.
"""
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_always_enhance as always
from .adaptive import operations as ops
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import EngineConfig
from .lowlight import LowLightUnavailable
from .models import ImageEnhancementResult
from .restoration import (
    AI_ENGINES, CLASSICAL_ENGINES, FINAL_LAYER_PREFIX, FINISH_VARIANT_PREFIX, describe_stages, engine_kind, processing_summary, winning_engine,
)
from .serializers import ImageEnhancementResultSerializer
from .test_adaptive_engine import analyzer_with, portrait, runner_denoise, runner_lift
from .test_pipeline_regressions import ResultFixture
from rest_framework.test import APIClient


def record(operation, engines, ran, status="accepted", variant="blend 1.0", reason="", **extra):
    """A stage record as the engine writes it: `models_run` is SORTED by name (not by order of use), `engines` is the preference order."""
    return {"operation": operation, "reason": reason, "engines": list(engines), "skipped_engines": [], "notes": [], "candidates": [], "optional": False,
            "models_run": sorted(ran), "status": status, "variant": variant, **extra}


def parameters(*stages):
    return {"adaptive": {"stages": list(stages), "quality_report": {"recommended_operations": []}, "operations_skipped": []}}


def final_layer(status="rejected"):
    return record(ops.DETAIL, ["general"], ["general"] if status == "rejected" else [], status=status, variant=None, reason=f"{FINAL_LAYER_PREFIX}: the enhancement model is applied to every photograph.")


def finishing_polish(variant="finish blend 0.65"):
    return record(ops.POLISH, ["opencv_polish"], ["opencv_polish"], variant=variant, reason="No defect was found; a gentle polish may still help.")


class WinningEngineTests(SimpleTestCase):
    def test_the_first_engine_wins_when_it_was_accepted(self):
        self.assertEqual(winning_engine(record(ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["zero_dce"])), "zero_dce")

    def test_the_engine_that_produced_the_result_is_named_not_the_alphabetically_first_one_that_ran(self):
        """`models_run` is sorted, so its first entry is a refused engine whenever a fallback won (the case `applied_stages` got wrong)."""
        cases = (
            (ops.DENOISE, ["nafnet_sidd", "opencv_nlm"], ["nafnet_sidd", "opencv_nlm"], "opencv_nlm"),
            (ops.FACE, ["codeformer", "gfpgan"], ["codeformer", "gfpgan"], "gfpgan"),
            (ops.LOW_LIGHT, ["retinexformer", "zero_dce"], ["retinexformer", "zero_dce"], "zero_dce"),
            (ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["zero_dce", "opencv_exposure"], "opencv_exposure"),
            (ops.DETAIL, ["general", "realesrgan"], ["general", "realesrgan"], "realesrgan"),
        )
        for operation, engines, ran, expected in cases:
            with self.subTest(engines=engines):
                self.assertEqual(winning_engine(record(operation, engines, ran)), expected)
                self.assertEqual(describe_stages(parameters(record(operation, engines, ran)))["applied_stages"][0]["engine"], expected)

    def test_an_engine_that_could_not_run_is_not_the_winner(self):
        stage = record(ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["opencv_exposure"], skipped_engines=[{"engine": "zero_dce", "reason": "the checkpoint is missing"}])
        self.assertEqual(winning_engine(stage), "opencv_exposure")

    def test_a_record_without_engines_keeps_the_first_model_run(self):
        self.assertEqual(winning_engine({"models_run": ["some_engine"]}), "some_engine")
        self.assertEqual(winning_engine({"models_run": ["a", "b"]}), "a")
        self.assertIsNone(winning_engine({"models_run": []}))
        self.assertIsNone(winning_engine({}))

    def test_odd_stored_values_do_not_raise(self):
        for stage in ({"engines": "zero_dce", "models_run": None}, {"engines": [None, 3], "models_run": [3]}, {"engines": {"a": 1}, "models_run": {"b": 2}}):
            winning_engine(stage)


class EngineKindTests(SimpleTestCase):
    def test_every_engine_the_adaptive_engine_can_run_is_classified_exactly_once(self):
        every = {engine for engines in ops.ENGINES.values() for engine in engines}
        self.assertEqual(AI_ENGINES | CLASSICAL_ENGINES, every)
        self.assertEqual(AI_ENGINES & CLASSICAL_ENGINES, set())

    def test_the_classical_engines_are_the_ones_without_a_model(self):
        self.assertEqual(CLASSICAL_ENGINES, {"opencv_exposure", "opencv_nlm", "opencv_polish"})
        self.assertTrue(all(engine_kind(engine) == "classical" for engine in CLASSICAL_ENGINES))
        self.assertTrue(all(engine_kind(engine) == "ai" for engine in AI_ENGINES))

    def test_an_engine_nobody_classified_is_never_called_ai(self):
        self.assertEqual(engine_kind("a_future_engine"), "unknown")
        self.assertEqual(engine_kind(None), "unknown")


@override_settings(**always.ON)
class RouterNamesTests(SimpleTestCase):
    """The summary recognises the finishing polish and the final layer by the names the router gives them; this pins both names to the router."""

    def test_the_final_layer_and_the_finishing_polish_are_named_as_the_summary_expects(self):
        router = AdaptiveEnhancementRouter(EngineConfig())
        report = analyzer_with().analyze(portrait())
        self.assertTrue(router.final_layer_step(report).reason.startswith(FINAL_LAYER_PREFIX))
        finish = router.finish_step(report)
        self.assertEqual(finish.operation, ops.POLISH)
        self.assertTrue(all(variant["name"].startswith(FINISH_VARIANT_PREFIX) for variant in finish.variants))

    @override_settings(ADAPTIVE_ALWAYS_ENHANCE=False)
    def test_the_ordinary_polish_is_not_a_finishing_polish(self):
        step = AdaptiveEnhancementRouter(EngineConfig()).next_step(analyzer_with().analyze(portrait()), set(), polish={"style": "natural"}).step
        self.assertEqual(step.operation, ops.POLISH)
        self.assertFalse(any(variant["name"].startswith(FINISH_VARIANT_PREFIX) for variant in step.variants))


class SummaryOfAStoredRecordTests(SimpleTestCase):
    def test_learned_models_only(self):
        stored = parameters(record(ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["zero_dce"]), record(ops.FACE, ["codeformer", "gfpgan"], ["codeformer"]),
                            record(ops.UPSCALE, ["realesrgan"], ["realesrgan"], variant="x2"))
        summary = processing_summary(stored)
        self.assertEqual(summary["ai_applied"], True)
        self.assertEqual(summary["classical_fallback"], False)
        self.assertEqual([(s["stage"], s["engine"], s["kind"], s["fallback"], s["replaced"]) for s in summary["stages"]],
                         [("low_light", "zero_dce", "ai", False, []), ("face_restore", "codeformer", "ai", False, []), ("upscale", "realesrgan", "ai", False, [])])

    def test_the_polish_is_classical_by_design_and_not_a_fallback(self):
        summary = processing_summary(parameters(record(ops.LOW_LIGHT, ["zero_dce"], ["zero_dce"]), record(ops.POLISH, ["opencv_polish"], ["opencv_polish"])))
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (True, False))
        self.assertEqual(summary["stages"][1], {"stage": "polish", "engine": "opencv_polish", "kind": "classical", "fallback": False, "replaced": []})

    def test_a_classical_engine_that_took_over_from_a_refused_model_is_a_classical_fallback(self):
        stored = parameters(record(ops.DENOISE, ["nafnet_sidd", "opencv_nlm"], ["nafnet_sidd", "opencv_nlm"]))
        summary = processing_summary(stored)
        self.assertEqual(summary["stages"], [{"stage": "denoise", "engine": "opencv_nlm", "kind": "classical", "fallback": True, "replaced": ["nafnet_sidd"]}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, True))

    def test_a_classical_engine_that_took_over_from_a_model_that_could_not_run_is_a_classical_fallback(self):
        stage = record(ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["opencv_exposure"], skipped_engines=[{"engine": "zero_dce", "reason": "ZERO_DCE_ENABLED is off"}])
        summary = processing_summary(parameters(stage))
        self.assertEqual(summary["stages"], [{"stage": "low_light", "engine": "opencv_exposure", "kind": "classical", "fallback": True, "replaced": ["zero_dce"]}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, True))

    def test_a_classical_stage_with_no_learned_model_switched_on_is_not_called_a_fallback(self):
        """Nothing stood ahead of it, so nothing fell back: the stage is classical (`kind`) and `ai_applied` is false."""
        summary = processing_summary(parameters(record(ops.LOW_LIGHT, ["opencv_exposure"], ["opencv_exposure"])))
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"], summary["stages"][0]["kind"], summary["stages"][0]["fallback"]), (False, False, "classical", False))

    def test_a_fallback_between_two_learned_models_is_a_fallback_but_not_a_classical_one(self):
        summary = processing_summary(parameters(record(ops.FACE, ["codeformer", "gfpgan"], ["codeformer", "gfpgan"])))
        self.assertEqual(summary["stages"], [{"stage": "face_restore", "engine": "gfpgan", "kind": "ai", "fallback": True, "replaced": ["codeformer"]}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (True, False))

    def test_a_mixed_result_says_both(self):
        stored = parameters(record(ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["zero_dce", "opencv_exposure"]), record(ops.FACE, ["codeformer"], ["codeformer"]))
        summary = processing_summary(stored)
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (True, True))
        self.assertEqual([s["kind"] for s in summary["stages"]], ["classical", "ai"])

    def test_the_finishing_polish_replaces_a_refused_final_layer(self):
        for status in ("rejected", "unavailable"):
            with self.subTest(final_layer=status):
                summary = processing_summary(parameters(final_layer(status), finishing_polish()))
                self.assertEqual(summary["stages"], [{"stage": "polish", "engine": "opencv_polish", "kind": "classical", "fallback": True, "replaced": ["general"]}])
                self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, True))

    def test_the_finishing_polish_with_no_final_layer_offered_replaced_nothing(self):
        summary = processing_summary(parameters(finishing_polish("finish")))
        self.assertEqual(summary["stages"], [{"stage": "polish", "engine": "opencv_polish", "kind": "classical", "fallback": False, "replaced": []}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, False))

    def test_a_refused_step_is_not_listed_and_does_not_count_as_applied(self):
        summary = processing_summary(parameters(record(ops.DENOISE, ["nafnet_sidd", "opencv_nlm"], ["nafnet_sidd", "opencv_nlm"], status="rejected"),
                                                record(ops.FACE, ["codeformer"], [], status="unavailable")))
        self.assertEqual(summary, {"ai_applied": False, "classical_fallback": False, "stages": []})

    def test_the_original_kept_is_an_empty_summary_not_a_missing_one(self):
        self.assertEqual(processing_summary(parameters()), {"ai_applied": False, "classical_fallback": False, "stages": []})

    def test_an_engine_nobody_classified_is_listed_but_counts_as_neither(self):
        summary = processing_summary(parameters(record("a_future_operation", ["a_future_engine"], ["a_future_engine"])))
        self.assertEqual(summary["stages"], [{"stage": "a_future_operation", "engine": "a_future_engine", "kind": "unknown", "fallback": False, "replaced": []}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, False))

    def test_there_is_nothing_to_report_for_a_failure_or_for_a_result_without_a_stage_record(self):
        stored = parameters(record(ops.LOW_LIGHT, ["zero_dce"], ["zero_dce"]))
        self.assertIsNone(processing_summary(stored, failed=True))
        for other in ({}, {"algorithm": "legacy"}, {"hd_restoration": {"applied": True}}, {"adaptive": "garbage"}, None, [], "text"):
            with self.subTest(parameters=other):
                self.assertIsNone(processing_summary(other))

    def test_odd_stage_records_do_not_raise(self):
        stored = {"adaptive": {"stages": ["text", None, {"status": "accepted"}, {"status": "accepted", "engines": "zero_dce", "models_run": 5}]}}
        summary = processing_summary(stored)
        self.assertEqual([item["engine"] for item in summary["stages"]], [None, None])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, False))
        self.assertEqual(processing_summary({"adaptive": {"stages": "not a list"}}), {"ai_applied": False, "classical_fallback": False, "stages": []})

    def test_applied_stages_and_the_summary_name_the_same_engines(self):
        stored = parameters(record(ops.DENOISE, ["nafnet_sidd", "opencv_nlm"], ["nafnet_sidd", "opencv_nlm"]), record(ops.FACE, ["codeformer", "gfpgan"], ["codeformer"]))
        self.assertEqual([s["engine"] for s in describe_stages(stored)["applied_stages"]], [s["engine"] for s in processing_summary(stored)["stages"]])


def stage_engines(summary):
    return [item["engine"] for item in summary["stages"]]


def chosen_models(parameters_):
    """What the engine itself recorded as the model of each accepted stage (the chosen candidate's model, in the `algorithm` string)."""
    return parameters_["algorithm"].split("+")[1:]


@override_settings(**always.ON)
class SummaryOfARealRunTests(SimpleTestCase):
    """The real adaptive engine with stand-in models: the summary agrees with the chosen model the engine recorded, stage by stage."""

    def run_engine(self, image, runners, **settings_):
        return always.run(image, runners=runners, analyzer=analyzer_with(), identity=False, **settings_)[2]

    def test_a_dark_photograph_lifted_by_the_first_model(self):
        stored = self.run_engine(portrait(brightness=0.12), {ops.LOW_LIGHT: runner_lift}, ADAPTIVE_ALWAYS_ENHANCE=False)
        summary = processing_summary(stored)
        self.assertEqual(stage_engines(summary), ["retinexformer"])
        self.assertEqual(stage_engines(summary), chosen_models(stored))
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"], summary["stages"][0]["fallback"]), (True, False, False))

    def test_the_first_model_cannot_run_and_the_classical_exposure_correction_takes_over(self):
        def runner(image, engine, variant):
            if engine == "retinexformer":
                raise LowLightUnavailable("the checkpoint is missing")
            return runner_lift(image, engine, variant)

        stored = self.run_engine(portrait(brightness=0.12), {ops.LOW_LIGHT: runner}, ADAPTIVE_ALWAYS_ENHANCE=False)
        summary = processing_summary(stored)
        self.assertEqual(stage_engines(summary), chosen_models(stored))
        self.assertEqual(summary["stages"], [{"stage": "low_light", "engine": "opencv_exposure", "kind": "classical", "fallback": True, "replaced": ["retinexformer"]}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, True))

    def test_the_first_model_is_refused_and_the_classical_exposure_correction_takes_over(self):
        def runner(image, engine, variant):
            if engine == "retinexformer":
                return np.full_like(image, 250), [{"model": engine}]          # refused by every gate
            return runner_lift(image, engine, variant)

        stored = self.run_engine(portrait(brightness=0.2), {ops.LOW_LIGHT: runner}, ADAPTIVE_ALWAYS_ENHANCE=False)
        summary = processing_summary(stored)
        self.assertEqual(stage_engines(summary), chosen_models(stored))
        self.assertEqual(summary["stages"][0]["fallback"], True)
        self.assertEqual(summary["stages"][0]["replaced"], ["retinexformer"])
        self.assertEqual(summary["classical_fallback"], True)

    def test_a_refused_denoising_model_is_named_correctly_when_the_classical_one_wins(self):
        """The case the sorted `models_run` got wrong: nafnet_sidd sorts before opencv_nlm, so the first model run was named although it was refused."""
        def runner(image, engine, variant):
            if engine == "nafnet_sidd":
                return image.copy(), [{"model": engine}]                     # changes nothing: refused
            return runner_denoise(image, engine, variant)

        stored = self.run_engine(portrait(noise=24.0), {ops.DENOISE: runner}, ADAPTIVE_ALWAYS_ENHANCE=False)
        stage = next(item for item in stored["adaptive"]["stages"] if item["operation"] == ops.DENOISE)
        self.assertEqual((stage["status"], stage["models_run"]), ("accepted", ["nafnet_sidd", "opencv_nlm"]))
        summary = processing_summary(stored)
        self.assertEqual(stage_engines(summary), chosen_models(stored))
        self.assertEqual(stage_engines(summary), ["opencv_nlm"])
        self.assertEqual([s["engine"] for s in describe_stages(stored)["applied_stages"]], ["opencv_nlm"])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, True))

    def test_the_final_layer_accepted_is_learned_and_not_a_fallback(self):
        stored = always.run(always.polish_tests.soft_portrait(), runners={ops.DETAIL: always.runner_detail_adds_structure})[2]
        summary = processing_summary(stored)
        self.assertEqual(summary["stages"], [{"stage": "detail", "engine": "general", "kind": "ai", "fallback": False, "replaced": []}])
        self.assertEqual(stage_engines(summary), chosen_models(stored))
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (True, False))

    def test_a_refused_final_layer_leaves_the_finishing_polish_and_the_summary_says_so(self):
        stored = always.run(always.polish_tests.soft_portrait(), runners={ops.DETAIL: always.runner_detail_changes_nothing})[2]
        summary = processing_summary(stored)
        self.assertEqual(summary["stages"], [{"stage": "polish", "engine": "opencv_polish", "kind": "classical", "fallback": True, "replaced": ["general"]}])
        self.assertEqual(stage_engines(summary), chosen_models(stored))
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, True))

    def test_no_final_layer_for_a_photograph_over_the_limit_leaves_a_classical_polish_that_replaced_nothing(self):
        stored = always.run(always.polish_tests.soft_portrait(), runners={ops.DETAIL: always.runner_detail_adds_structure}, ADAPTIVE_DETAIL_MAX_PIXELS=1000)[2]
        summary = processing_summary(stored)
        self.assertEqual(summary["stages"], [{"stage": "polish", "engine": "opencv_polish", "kind": "classical", "fallback": False, "replaced": []}])
        self.assertEqual((summary["ai_applied"], summary["classical_fallback"]), (False, False))

    def test_a_photograph_the_engine_left_alone_has_an_empty_summary(self):
        stored = always.run(portrait(), runners={ops.LOW_LIGHT: runner_lift}, analyzer=analyzer_with(), identity=False, ADAPTIVE_ALWAYS_ENHANCE=False)[2]
        self.assertEqual(stored["algorithm"], "adaptive_v1+original_preserved")
        self.assertEqual(processing_summary(stored), {"ai_applied": False, "classical_fallback": False, "stages": []})


class SummaryInTheApiTests(ResultFixture):
    def completed(self, stored, status="completed"):
        result = self.make_result(status=status)
        ImageEnhancementResult.objects.filter(pk=result.pk).update(applied_parameters=stored, status=status)
        return ImageEnhancementResult.objects.get(pk=result.pk)

    def test_the_result_serializer_carries_the_summary(self):
        stored = parameters(record(ops.LOW_LIGHT, ["zero_dce", "opencv_exposure"], ["opencv_exposure", "zero_dce"]))
        data = ImageEnhancementResultSerializer(self.completed(stored)).data
        self.assertEqual(data["processing_summary"], {
            "ai_applied": False, "classical_fallback": True,
            "stages": [{"stage": "low_light", "engine": "opencv_exposure", "kind": "classical", "fallback": True, "replaced": ["zero_dce"]}]})
        self.assertEqual(data["applied_stages"][0]["engine"], "opencv_exposure")         # the older field names the same engine

    def test_the_result_endpoint_returns_it(self):
        client = APIClient()
        client.force_authenticate(self.user)
        result = self.completed(parameters(record(ops.FACE, ["codeformer", "gfpgan"], ["codeformer"])))
        body = client.get(f"/api/enhancements/results/{result.pk}/").json()
        self.assertEqual(body["processing_summary"]["ai_applied"], True)
        self.assertEqual(body["processing_summary"]["stages"][0]["engine"], "codeformer")

    def test_it_is_null_when_there_is_no_record_to_read(self):
        for label, result in (
            ("failed", self.completed(parameters(record(ops.LOW_LIGHT, ["zero_dce"], ["zero_dce"])), status="failed")),
            ("pending", self.completed({}, status="queued")),
            ("staged pipeline", self.completed({"algorithm": "legacy", "hd_restoration": {"applied": True}})),
        ):
            with self.subTest(label):
                self.assertIsNone(ImageEnhancementResultSerializer(result).data["processing_summary"])

    def test_results_stored_before_the_field_existed_still_serialize(self):
        result = self.completed({"adaptive": {"outcome": "enhanced", "stages": [{"operation": "low_light_enhancement", "status": "accepted", "models_run": ["zero_dce"], "variant": "blend 1.0"}]}})
        data = ImageEnhancementResultSerializer(result).data
        self.assertEqual(data["processing_summary"]["stages"][0]["engine"], "zero_dce")
        self.assertEqual(data["processing_summary"]["stages"][0]["kind"], "ai")

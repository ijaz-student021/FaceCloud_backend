"""`applied_stages[].stage` is never null: every operation the adaptive engine can run has an API stage name (a missing one sent `null` and broke the image screen)."""
from django.test import SimpleTestCase

from .adaptive import operations as ops
from .restoration import OPERATION_STAGE, describe_stages


def parameters(*stages):
    return {"adaptive": {"stages": list(stages), "quality_report": {"recommended_operations": []}, "operations_skipped": []}}


def accepted(operation, engine="general", variant="final 0.7"):
    return {"operation": operation, "status": "accepted", "models_run": [engine], "variant": variant}


class StageNameTests(SimpleTestCase):
    def test_every_operation_the_engine_can_run_has_a_stage_name(self):
        self.assertEqual(set(ops.ORDER) - set(OPERATION_STAGE), set())

    def test_the_names_are_unique_and_never_empty(self):
        self.assertEqual(len(set(OPERATION_STAGE.values())), len(OPERATION_STAGE))
        self.assertTrue(all(OPERATION_STAGE.values()))

    def test_the_detail_model_and_colourization_are_reported_under_their_names(self):
        described = describe_stages(parameters(accepted(ops.DETAIL), accepted(ops.COLORIZE, "ddcolor", "natural colour")))
        self.assertEqual(
            described["applied_stages"],
            [{"stage": "detail", "engine": "general", "variant": "final 0.7"}, {"stage": "colourize", "engine": "ddcolor", "variant": "natural colour"}],
        )

    def test_every_operation_gives_a_text_stage(self):
        for operation in ops.ORDER:
            for item in describe_stages(parameters(accepted(operation)))["applied_stages"]:
                self.assertIsInstance(item["stage"], str)
                self.assertTrue(item["stage"])

    def test_an_operation_the_table_does_not_know_is_reported_under_its_own_name(self):
        described = describe_stages(parameters(accepted("a_future_operation", "some_engine", "v1")))
        self.assertEqual(described["applied_stages"], [{"stage": "a_future_operation", "engine": "some_engine", "variant": "v1"}])
        self.assertEqual(describe_stages(parameters(accepted(None)))["applied_stages"][0]["stage"], "unknown")

    def test_a_stored_result_made_before_the_fix_is_described_correctly_now(self):
        """The names are made when the result is read, so a result stored with the old engine output needs no repair."""
        stored = parameters({"operation": "detail_restoration", "status": "accepted", "models_run": ["general"], "variant": "refine 0.7", "seconds": 3.1})
        self.assertEqual(describe_stages(stored)["applied_stages"][0]["stage"], "detail")

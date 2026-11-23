"""ADAPTIVE_EDGE_BOOST: the Auto preset sharpens the edges of the finished photograph once more (off by default).

Measured with the engine's own halo gate on four real portraits before it was written (see config/settings.py); these tests are about the decisions: when the step is planned, what it asks
for, that every gate still judges it, and that nothing changes while the setting is 0.
"""
import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_always_enhance as always
from . import test_adaptive_polish as polish_tests
from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import EngineConfig


def sharpness(image):
    return float(cv2.Laplacian(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


@override_settings(**always.ON)
class RouterTests(SimpleTestCase):
    def report(self):
        return polish_tests.skin_analyzer().analyze(polish_tests.soft_portrait())

    def step(self, style="natural", level="auto"):
        return AdaptiveEnhancementRouter(EngineConfig()).edge_boost_step(self.report(), profile_for(style, level))

    def test_it_is_off_by_default(self):
        self.assertIsNone(self.step())

    @override_settings(ADAPTIVE_EDGE_BOOST=0.8)
    def test_it_is_a_polish_with_a_ladder_that_changes_no_colour(self):
        step = self.step()
        self.assertEqual((step.operation, step.optional), (ops.POLISH, True))
        self.assertEqual([variant["amount"] for variant in step.variants], [0.8, 0.52, 0.32])          # strongest first; the validator keeps the strongest that passes every gate
        self.assertTrue(all(variant["vibrance"] == 0.0 and variant["blend"] == 1.0 for variant in step.variants))
        self.assertTrue(all(variant["edge_boost"] for variant in step.variants))                       # the validator judges it on the frame's edges, not on the mean change
        self.assertTrue(all(variant["clahe"] <= 0.1 for variant in step.variants))                       # edges, not a contrast change
        self.assertTrue(step.reason.startswith("Edge boost"))

    @override_settings(ADAPTIVE_EDGE_BOOST=0.8)
    def test_only_the_auto_preset_gets_it(self):
        for style, level in (("ai_max", "high"), ("natural", "high"), ("dynamic", "auto"), ("natural", "low")):
            self.assertIsNone(self.step(style, level), (style, level))


@override_settings(**always.ON)
class EngineTests(SimpleTestCase):
    def source(self):
        return polish_tests.soft_portrait()

    def run_with(self, boost, style="natural"):
        with override_settings(ADAPTIVE_EDGE_BOOST=boost):
            return always.run(self.source(), runners={ops.DETAIL: always.runner_detail_adds_structure}, style=style)

    def test_while_the_setting_is_zero_nothing_changes(self):
        _out, _level, parameters = self.run_with(0.0)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.DETAIL])

    def test_with_it_on_the_edges_are_sharpened_after_the_final_layer(self):
        plain, _l, plain_parameters = self.run_with(0.0)
        boosted, _l, parameters = self.run_with(0.8)
        adaptive = parameters["adaptive"]
        self.assertEqual(adaptive["operations_applied"], [ops.DETAIL, ops.POLISH])
        self.assertTrue(next(stage for stage in adaptive["stages"] if stage["operation"] == ops.POLISH)["variant"].startswith("edge boost"))
        self.assertGreater(sharpness(boosted), sharpness(plain))
        self.assertEqual(plain_parameters["adaptive"]["outcome"], "enhanced")

    def test_ai_enhance_does_not_get_the_extra_step(self):
        _out, _level, parameters = self.run_with(0.8, style="ai_max")
        self.assertNotIn("edge boost", " ".join(str(stage.get("variant")) for stage in parameters["adaptive"]["stages"]))

    @override_settings(ADAPTIVE_EDGE_BOOST=0.8)
    def test_the_gates_still_judge_it_a_refused_boost_leaves_the_photograph_as_it_was(self):
        refusing = polish_tests.FakeIdentity

        class Changed(refusing):
            def check(self, candidate, restoring=False):
                return {"status": "failed", "similarity": 0.2, "threshold": self.threshold}

        from unittest.mock import patch

        from .adaptive.service import AdaptiveImageEnhancementService

        service = AdaptiveImageEnhancementService(analyzer=polish_tests.skin_analyzer(), config=EngineConfig(), runners={ops.DETAIL: always.runner_detail_adds_structure})
        with patch("enhancement.adaptive.service.IdentityChecker", Changed):
            _out, _level, parameters = service.enhance(self.source(), style="natural")
        self.assertNotIn(ops.POLISH, parameters["adaptive"]["operations_applied"])
        self.assertIn(parameters["adaptive"]["outcome"], ("enhanced", "partially_enhanced", "original_preserved"))
        self.assertTrue(np.all(np.isfinite(_out)))

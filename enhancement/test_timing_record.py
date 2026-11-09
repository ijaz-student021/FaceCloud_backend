"""Where the time goes is recorded with the result: the analysis of the original, and for every candidate the model's seconds and the validator's seconds."""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .adaptive import operations as ops
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait, runner_lift


@override_settings(**ALL_ON, ADAPTIVE_ALWAYS_ENHANCE=False)
class TimingRecordTests(SimpleTestCase):
    def run_engine(self):
        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(), config=EngineConfig(), runners={ops.LOW_LIGHT: runner_lift})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            return service.enhance(portrait(brightness=0.12))[2]["adaptive"]

    def test_the_analysis_and_every_candidate_carry_seconds(self):
        adaptive = self.run_engine()
        self.assertGreater(adaptive["analysis_seconds"], 0)
        self.assertLessEqual(adaptive["analysis_seconds"], adaptive["seconds"])
        candidates = [item for item in adaptive["candidates"] if item["operation"] != "original"]
        self.assertTrue(candidates)
        for item in candidates:
            self.assertGreaterEqual(item["seconds"], 0)
            self.assertGreaterEqual(item["validation_seconds"], 0)
        self.assertTrue(any(item["validation_seconds"] > 0 for item in candidates))          # the validator really ran for them

    def test_what_is_accounted_for_does_not_exceed_the_total(self):
        adaptive = self.run_engine()
        accounted = adaptive["analysis_seconds"] + sum(item["seconds"] + item["validation_seconds"] for item in adaptive["candidates"])
        self.assertLessEqual(accounted, adaptive["seconds"] + 0.05)          # rounding to hundredths

    def test_a_released_candidate_keeps_its_timing(self):
        from .adaptive.candidates import Candidate

        import numpy as np

        candidate = Candidate(id="c1", image=np.zeros((4, 4, 3), np.uint8), parent="c0", operation="polish", seconds=1.5, validation_seconds=0.25)
        candidate.release()
        self.assertEqual((candidate.describe()["seconds"], candidate.describe()["validation_seconds"]), (1.5, 0.25))

"""Where the engine's time went, and what was done about it (measured on an Apple M1, 750 x 1000 px, natural / auto):

* NAFNet deblurring ran its network once per blend (13.2 s, then 10.2 s for the 0.6 blend): the output is now kept for the blends of one step.
* When the compact detail network was refused, Real-ESRGAN x4plus ran at 2x for 44 s: it is now skipped above ``ADAPTIVE_SLOW_DETAIL_MAX_PIXELS`` and the result says so.

Stand-ins replace the networks (CI has no weights); the real timings are in the pull request that made the change.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.candidates import CandidatePool
from .adaptive.router import Step
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .test_adaptive_detail import ON, HeavyCompressionAnalyzer, sharpened
from .test_adaptive_polish import _no_identity


def photo(height=60, width=80):
    return np.random.default_rng(3).integers(0, 255, (height, width, 3), dtype=np.uint8)


class NetworkOutputReuseTests(SimpleTestCase):
    def setUp(self):
        ops.clear_detail_cache()
        self.addCleanup(ops.clear_detail_cache)

    def test_deblurring_runs_its_network_once_for_all_the_blends(self):
        image = photo()
        raw = sharpened(image, 0.5)
        with patch("enhancement.hd_restoration.deblur_with_nafnet", return_value=(raw, {"provider": "stand-in"})) as network:
            full, first = ops.run_deblur(image, "nafnet_gopro", 1.0)
            partly, second = ops.run_deblur(image, "nafnet_gopro", 0.6)
        self.assertEqual(network.call_count, 1)
        self.assertNotIn("model_reused", first[0])
        self.assertIs(second[0]["model_reused"], True)
        np.testing.assert_array_equal(full, raw)
        np.testing.assert_array_equal(partly, cv2.addWeighted(raw, 0.6, image, 0.4, 0))

    def test_the_kept_output_belongs_to_one_input_image(self):
        image = photo()
        with patch("enhancement.hd_restoration.deblur_with_nafnet", side_effect=lambda x: (x, {})) as network:
            ops.run_deblur(image, "nafnet_gopro", 1.0)
            ops.run_deblur(image.copy(), "nafnet_gopro", 1.0)          # an equal photograph that is not the same object is a different input
        self.assertEqual(network.call_count, 2)

    def test_it_is_forgotten_when_a_run_ends(self):
        image = photo()
        with patch("enhancement.hd_restoration.deblur_with_nafnet", side_effect=lambda x: (x, {})) as network:
            ops.run_deblur(image, "nafnet_gopro", 1.0)
            ops.clear_detail_cache()
            ops.run_deblur(image, "nafnet_gopro", 1.0)
        self.assertEqual(network.call_count, 2)

    def test_denoising_reuses_its_output_too_and_each_operation_keeps_its_own(self):
        image = photo()
        with patch("enhancement.services._edge_aware_denoise", side_effect=lambda x, _strength: x) as denoise, \
                patch("enhancement.hd_restoration.deblur_with_nafnet", side_effect=lambda x: (x, {})) as deblur:
            ops.run_denoise(image, "opencv_nlm", 1.0)
            ops.run_deblur(image, "nafnet_gopro", 1.0)
            _out, second = ops.run_denoise(image, "opencv_nlm", 0.5)
        self.assertEqual((denoise.call_count, deblur.call_count), (1, 1))
        self.assertIs(second[0]["model_reused"], True)

    def test_a_different_engine_for_the_same_image_is_not_served_from_the_kept_output(self):
        image = photo()
        with patch("enhancement.hd_restoration.denoise_with_nafnet", side_effect=lambda x: (x, {})) as network, \
                patch("enhancement.services._edge_aware_denoise", side_effect=lambda x, _strength: x) as classical:
            ops.run_denoise(image, "nafnet_sidd", 1.0)
            ops.run_denoise(image, "opencv_nlm", 1.0)
        self.assertEqual((network.call_count, classical.call_count), (1, 1))


class SlowDetailLimitTests(SimpleTestCase):
    def propose(self, engines, limit, runner):
        image = photo(40, 50)                          # 2,000 pixels
        service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(None), config=EngineConfig(), runners={ops.DETAIL: runner})
        pool = CandidatePool(image, 8)
        report = service._analyzer().analyze(image)

        class Refuses:
            def evaluate(self, *_args, **_kwargs):
                return {"accepted": False, "reasons": ["stand-in"], "gates": {}, "improvement": 0.0, "change_cost": 0.0}

        record = {"operation": ops.DETAIL, "skipped_engines": [], "notes": [], "candidates": []}
        step = Step(ops.DETAIL, engines, [{"name": "blend 1.0", "blend": 1.0}], "test", optional=True)
        with override_settings(ADAPTIVE_SLOW_DETAIL_MAX_PIXELS=limit):
            produced = service._propose(step, pool.original, report, pool, Refuses(), None, record)
        return produced, record

    def runner(self, calls):
        def run(image, engine, variant):
            calls.append(engine)
            return sharpened(image, 0.3), [{"model": engine}]
        return run

    def test_the_slow_network_is_not_run_on_a_photograph_above_the_limit(self):
        calls = []
        produced, record = self.propose(["general", "realesrgan"], 1_000, self.runner(calls))
        self.assertEqual(calls, ["general"])
        self.assertEqual(len(produced), 1)
        (skipped,) = record["skipped_engines"]
        self.assertEqual((skipped["engine"], skipped["size_limit"]), ("realesrgan", True))
        self.assertIn("slow detail model was not used", skipped["reason"])
        self.assertIn("0.00 megapixels", skipped["reason"])

    def test_it_runs_as_the_fallback_when_the_photograph_is_within_the_limit(self):
        calls = []
        _produced, record = self.propose(["general", "realesrgan"], 5_000, self.runner(calls))
        self.assertEqual(calls, ["general", "realesrgan"])
        self.assertEqual(record["skipped_engines"], [])

    def test_4x_ultrasharp_as_a_fallback_is_limited_too(self):
        calls = []
        _produced, record = self.propose(["general", "realesrgan", "ultrasharp"], 1_000, self.runner(calls))
        self.assertEqual(calls, ["general"])
        self.assertEqual([item["engine"] for item in record["skipped_engines"]], ["realesrgan", "ultrasharp"])

    def test_a_style_that_asks_for_the_slow_network_first_still_gets_it(self):
        calls = []
        _produced, record = self.propose(["ultrasharp", "general"], 1_000, self.runner(calls))
        self.assertEqual(calls, ["ultrasharp", "general"])
        self.assertEqual(record["skipped_engines"], [])

    def test_zero_means_no_limit(self):
        calls = []
        self.propose(["general", "realesrgan"], 0, self.runner(calls))
        self.assertEqual(calls, ["general", "realesrgan"])

    def test_only_the_detail_step_is_limited(self):
        calls = []
        image = photo(40, 50)
        service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(None), config=EngineConfig(), runners={ops.UPSCALE: lambda i, e, v: (calls.append(e) or i, [])})
        pool = CandidatePool(image, 8)
        record = {"operation": ops.UPSCALE, "skipped_engines": [], "notes": [], "candidates": []}

        class Refuses:
            def evaluate(self, *_args, **_kwargs):
                return {"accepted": False, "reasons": [], "gates": {}, "improvement": 0.0, "change_cost": 0.0}

        with override_settings(ADAPTIVE_SLOW_DETAIL_MAX_PIXELS=10):
            service._propose(Step(ops.UPSCALE, ["realesrgan"], [{"name": "x2", "scale": 2}], "test"), pool.original, service._analyzer().analyze(image), pool, Refuses(), None, record)
        self.assertEqual(calls, ["realesrgan"])

    def test_the_default_limit_keeps_a_1000_px_photograph_near_the_target(self):
        self.assertEqual(settings.ADAPTIVE_SLOW_DETAIL_MAX_PIXELS, 500_000)
        self.assertGreater(750 * 1000, settings.ADAPTIVE_SLOW_DETAIL_MAX_PIXELS)


@override_settings(**dict(ON, ADAPTIVE_POLISH_ENABLED=False), ADAPTIVE_ALWAYS_ENHANCE=False)
class SlowDetailLimitInTheEngineTests(SimpleTestCase):
    def run_engine(self, limit, engines=None):
        source = base.portrait()
        analyzer = HeavyCompressionAnalyzer(source, detector=base.StubDetector([]))
        calls = []

        def run(image, engine, variant):
            calls.append(engine)
            return image.copy(), [{"model": engine}]          # changes nothing, so the validator refuses it and the fallback would be tried

        service = AdaptiveImageEnhancementService(analyzer=analyzer, config=EngineConfig(), runners={ops.DETAIL: run})
        real = ops.enabled_engines
        patcher = patch("enhancement.adaptive.operations.enabled_engines", side_effect=lambda op: engines if engines is not None and op == ops.DETAIL else real(op))
        with patcher, patch("enhancement.adaptive.service.IdentityChecker", _no_identity), override_settings(ADAPTIVE_SLOW_DETAIL_MAX_PIXELS=limit):
            _out, _level, parameters = service.enhance(source)
        return calls, parameters["adaptive"]

    def test_a_large_photograph_is_not_given_the_slow_fallback_and_the_result_says_so(self):
        calls, adaptive = self.run_engine(1_000)
        self.assertEqual(calls, ["general", "general"])             # the compact network's two blends only
        reasons = [item["reason"] for item in adaptive["operations_skipped"] if item["operation"] == ops.DETAIL]
        self.assertEqual(len(reasons), 1)
        self.assertIn("slow detail model was not used", reasons[0])

    def test_with_only_the_slow_network_available_it_is_the_first_choice_and_runs(self):
        calls, adaptive = self.run_engine(1_000, engines=["realesrgan"])
        self.assertEqual(calls, ["realesrgan", "realesrgan"])
        self.assertFalse([item for item in adaptive["operations_skipped"] if "slow detail model" in item["reason"]])

    def test_within_the_limit_the_fallback_still_runs(self):
        calls, adaptive = self.run_engine(0)
        self.assertEqual(calls, ["general", "general", "realesrgan", "realesrgan"])
        self.assertFalse([item for item in adaptive["operations_skipped"] if "slow detail model" in item["reason"]])

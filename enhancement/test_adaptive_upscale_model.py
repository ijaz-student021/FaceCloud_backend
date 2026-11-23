"""ADAPTIVE_UPSCALE_MODEL: which Real-ESRGAN network makes the engine's enlargement (the compact one is about 13 times faster, measured; x4plus stays the default and the fallback)."""
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from enhancement.adaptive import operations as ops


@override_settings(HD_RESTORATION_ENABLED=True)
class UpscaleModelChoiceTests(SimpleTestCase):
    def test_the_default_is_the_x4plus_network_alone(self):
        self.assertEqual(ops.enabled_engines(ops.UPSCALE), ["realesrgan"])

    @override_settings(ADAPTIVE_UPSCALE_MODEL="general")
    def test_general_puts_the_compact_network_first_and_keeps_x4plus_as_the_fallback(self):
        self.assertEqual(ops.enabled_engines(ops.UPSCALE), ["general", "realesrgan"])

    @override_settings(ADAPTIVE_UPSCALE_MODEL="general", HD_RESTORATION_ENABLED=False)
    def test_with_the_models_switched_off_there_is_no_engine_either_way(self):
        self.assertEqual(ops.enabled_engines(ops.UPSCALE), [])

    def test_the_other_operations_are_not_touched(self):
        before = {operation: ops.enabled_engines(operation) for operation in ops.ORDER if operation != ops.UPSCALE}
        with override_settings(ADAPTIVE_UPSCALE_MODEL="general"):
            after = {operation: ops.enabled_engines(operation) for operation in ops.ORDER if operation != ops.UPSCALE}
        self.assertEqual(before, after)


@override_settings(HD_RESTORATION_ENABLED=True, REALESRGAN_OUTPUT_BLEND=0.35)
class RunUpscaleTests(SimpleTestCase):
    image = np.full((20, 30, 3), 100, np.uint8)

    def run_with(self, engine):
        model = np.full((40, 60, 3), 200, np.uint8)
        with patch("enhancement.hd_restoration.upscale_with_general", return_value=(model, {"provider": "general"})) as general, \
                patch("enhancement.hd_restoration.upscale_with_realesrgan", return_value=(model, {"provider": "x4plus"})) as heavy:
            result, records = ops.run_upscale(self.image, engine, 2)
        return result, records, general, heavy

    def test_the_general_engine_runs_the_compact_network_only(self):
        _result, _records, general, heavy = self.run_with("general")
        self.assertEqual((general.call_count, heavy.call_count), (1, 0))

    def test_the_realesrgan_engine_runs_x4plus_only(self):
        _result, _records, general, heavy = self.run_with("realesrgan")
        self.assertEqual((general.call_count, heavy.call_count), (0, 1))

    def test_both_are_blended_with_the_plain_enlargement_at_the_same_share(self):
        general, *_ = self.run_with("general")
        heavy, *_ = self.run_with("realesrgan")
        np.testing.assert_array_equal(general, heavy)
        self.assertEqual(general.shape, (40, 60, 3))
        self.assertAlmostEqual(float(general.mean()), 0.35 * 200 + 0.65 * 100, delta=1.0)

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from .purescale import PureScaleUnavailable, _load_model, upscale_with_purescale
from .services import enhance_bgr


class PureScaleAdapterTests(SimpleTestCase):
    def tearDown(self):
        from . import purescale

        purescale._MODEL_CACHE.clear()

    @override_settings(PURESCALE_MODEL_PATH="missing-purescale.pth", PURESCALE_MODEL_SHA256="0" * 64)
    def test_missing_checkpoint_fails_before_any_runtime_import(self):
        with (
            self.assertRaisesRegex(PureScaleUnavailable, "checkpoint is unavailable"),
            patch("enhancement.purescale._runtime") as runtime,
        ):
            _load_model()
        runtime.assert_not_called()

    def test_checksum_is_verified_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "PureScale_2x.pth"
            model_path.write_bytes(b"not-the-real-checkpoint")
            with (
                override_settings(PURESCALE_MODEL_PATH=str(model_path), PURESCALE_MODEL_SHA256="0" * 64),
                patch("enhancement.purescale._runtime") as runtime,
            ):
                with self.assertRaisesRegex(PureScaleUnavailable, "integrity verification"):
                    _load_model()
            runtime.assert_not_called()

    @override_settings(PURESCALE_MAX_OUTPUT_PIXELS=100)
    def test_oversized_request_is_rejected_before_loading_the_model(self):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        with patch("enhancement.purescale._load_model") as load_model:
            with self.assertRaisesRegex(PureScaleUnavailable, "safety limit"):
                upscale_with_purescale(source)
        load_model.assert_not_called()


class PureScalePipelineTests(SimpleTestCase):
    @override_settings(
        PURESCALE_ENABLED=False, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.upscale_with_purescale")
    def test_disabled_rollout_never_calls_the_provider(self, provider):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["purescale"]["applied"])
        self.assertIn("disabled", parameters["purescale"]["fallback_reason"])
        provider.assert_not_called()

    @override_settings(
        PURESCALE_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.upscale_with_purescale")
    def test_enabled_purescale_produces_a_2x_auditable_output(self, provider):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        upscaled = np.full((128, 192, 3), 105, dtype=np.uint8)
        provider.return_value = (upscaled, {"provider": "purescale_safmn_2x", "native_scale": 2})
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, (128, 192, 3))
        self.assertTrue(parameters["purescale"]["applied"])
        self.assertIn("purescale_safmn_2x", parameters["algorithm"])

    @override_settings(
        PURESCALE_ENABLED=True, HD_RESTORATION_ENABLED=True, NAFNET_DEBLUR_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.upscale_with_purescale")
    @patch("enhancement.services.upscale_with_realesrgan")
    def test_purescale_never_stacks_with_hd_restoration(self, realesrgan, purescale_provider):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        realesrgan.return_value = (
            np.full((128, 192, 3), 105, dtype=np.uint8),
            {"provider": "realesrgan_x4plus", "output_scale": 2},
        )
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, (128, 192, 3))
        self.assertTrue(parameters["hd_restoration"]["applied"])
        self.assertFalse(parameters["purescale"]["applied"])
        self.assertIn("already produced", parameters["purescale"]["fallback_reason"])
        purescale_provider.assert_not_called()

    @override_settings(
        PURESCALE_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.upscale_with_purescale")
    def test_provider_failure_preserves_pipeline_output(self, provider):
        provider.side_effect = PureScaleUnavailable("PureScale inference failed safely.")
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["purescale"]["applied"])
        self.assertEqual(parameters["purescale"]["fallback_reason"], "PureScale inference failed safely.")

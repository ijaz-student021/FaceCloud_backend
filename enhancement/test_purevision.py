import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from .purevision import PureVisionUnavailable, _load_model, restore_with_purevision
from .services import enhance_bgr


class PureVisionAdapterTests(SimpleTestCase):
    def tearDown(self):
        from . import purevision

        purevision._MODEL_CACHE.clear()

    @override_settings(PUREVISION_MODEL_PATH="missing-purevision.pth", PUREVISION_MODEL_SHA256="0" * 64)
    def test_missing_checkpoint_fails_before_any_runtime_import(self):
        with (
            self.assertRaisesRegex(PureVisionUnavailable, "checkpoint is unavailable"),
            patch("enhancement.purevision._runtime") as runtime,
        ):
            _load_model()
        runtime.assert_not_called()

    def test_checksum_is_verified_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "PureVision_1x.pth"
            model_path.write_bytes(b"not-the-real-checkpoint")
            with (
                override_settings(PUREVISION_MODEL_PATH=str(model_path), PUREVISION_MODEL_SHA256="0" * 64),
                patch("enhancement.purevision._runtime") as runtime,
            ):
                with self.assertRaisesRegex(PureVisionUnavailable, "integrity verification"):
                    _load_model()
            runtime.assert_not_called()

    @override_settings(PUREVISION_MAX_MEAN_CHROMA_SHIFT=8.0, PUREVISION_MAX_MEAN_LUMINANCE_SHIFT=30.0)
    @patch("enhancement.purevision._load_model")
    @patch("enhancement.purevision._tiled_inference")
    def test_provider_rejects_candidates_that_fail_the_fidelity_guard(self, tiled_inference, load_model):
        source = np.full((64, 96, 3), 80, dtype=np.uint8)
        load_model.return_value = (object(), "cpu")
        tiled_inference.return_value = np.full((64, 96, 3), 250, dtype=np.uint8)
        with self.assertRaisesRegex(PureVisionUnavailable, "fidelity guard"):
            restore_with_purevision(source)


class PureVisionPipelineTests(SimpleTestCase):
    @override_settings(
        PUREVISION_ENABLED=False, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.restore_with_purevision")
    def test_disabled_rollout_never_calls_the_provider(self, provider):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["purevision"]["applied"])
        self.assertIn("disabled", parameters["purevision"]["fallback_reason"])
        provider.assert_not_called()

    @override_settings(
        PUREVISION_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.restore_with_purevision")
    def test_enabled_purevision_preserves_the_pixel_grid(self, provider):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        cleaned = np.full((64, 96, 3), 102, dtype=np.uint8)
        provider.return_value = (cleaned, {"provider": "purevision_rrdb_1x"})
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertTrue(parameters["purevision"]["applied"])
        self.assertIn("purevision_rrdb_1x", parameters["algorithm"])

    @override_settings(
        PUREVISION_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
        EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD=25.0,
    )
    @patch("enhancement.services.restore_with_purevision")
    def test_extremely_dark_source_skips_purevision_even_when_enabled(self, provider):
        source = np.full((64, 96, 3), 12, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["purevision"]["applied"])
        self.assertIn("too dark", parameters["purevision"]["fallback_reason"])
        provider.assert_not_called()

    @override_settings(
        PUREVISION_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.restore_with_purevision")
    def test_provider_failure_preserves_pipeline_output(self, provider):
        provider.side_effect = PureVisionUnavailable("PureVision inference failed safely.")
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["purevision"]["applied"])
        self.assertEqual(parameters["purevision"]["fallback_reason"], "PureVision inference failed safely.")

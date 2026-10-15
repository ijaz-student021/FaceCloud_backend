import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from .colorization import ColorizationUnavailable, _load_model, colorize_with_siggraph17
from .services import enhance_bgr


def _grayscale_bgr(height=64, width=96, value=120):
    gray = np.full((height, width), value, dtype=np.uint8)
    cv2.rectangle(gray, (10, 10), (40, 40), value + 40, -1)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _color_bgr(height=64, width=96):
    image = np.full((height, width, 3), (60, 90, 180), dtype=np.uint8)
    cv2.circle(image, (48, 32), 15, (200, 60, 40), -1)
    return image


class ColorizationAdapterTests(SimpleTestCase):
    def tearDown(self):
        from . import colorization

        colorization._MODEL_CACHE.clear()

    @override_settings(COLORIZATION_MODEL_PATH="missing-colorization.pth", COLORIZATION_MODEL_SHA256="0" * 64)
    def test_missing_checkpoint_fails_before_any_runtime_import(self):
        with (
            self.assertRaisesRegex(ColorizationUnavailable, "checkpoint is unavailable"),
            patch("enhancement.colorization._runtime") as runtime,
        ):
            _load_model()
        runtime.assert_not_called()

    def test_checksum_is_verified_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "colorization_siggraph17.pth"
            model_path.write_bytes(b"not-the-real-checkpoint")
            with (
                override_settings(COLORIZATION_MODEL_PATH=str(model_path), COLORIZATION_MODEL_SHA256="0" * 64),
                patch("enhancement.colorization._runtime") as runtime,
            ):
                with self.assertRaisesRegex(ColorizationUnavailable, "integrity verification"):
                    _load_model()
            runtime.assert_not_called()

    @override_settings(COLORIZATION_MAX_MEAN_LUMINANCE_SHIFT=6.0)
    @patch("enhancement.colorization._load_model")
    @patch("enhancement.colorization._predict_ab")
    def test_provider_rejects_candidates_that_fail_the_luminance_guard(self, predict_ab, load_model):
        source = _grayscale_bgr()
        load_model.return_value = (object(), "cpu")
        # A wildly implausible ab prediction drives strong LAB->RGB gamut
        # clipping, which is exactly the failure mode this guard exists for.
        predict_ab.return_value = np.full((*source.shape[:2], 2), 127.0, dtype=np.float32)
        with self.assertRaisesRegex(ColorizationUnavailable, "luminance-fidelity guard"):
            colorize_with_siggraph17(source)

    @override_settings(COLORIZATION_MAX_INPUT_PIXELS=100)
    def test_oversized_request_is_rejected_before_loading_the_model(self):
        source = _grayscale_bgr()
        with patch("enhancement.colorization._load_model") as load_model:
            with self.assertRaisesRegex(ColorizationUnavailable, "safety limit"):
                colorize_with_siggraph17(source)
        load_model.assert_not_called()


class ColorizationPipelineTests(SimpleTestCase):
    @override_settings(
        COLORIZATION_ENABLED=False, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    )
    @patch("enhancement.services.colorize_with_siggraph17")
    def test_disabled_rollout_never_calls_the_provider(self, provider):
        source = _grayscale_bgr()
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["colorization"]["applied"])
        self.assertIn("disabled", parameters["colorization"]["fallback_reason"])
        provider.assert_not_called()

    @override_settings(
        COLORIZATION_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.colorize_with_siggraph17")
    def test_already_colour_source_never_calls_the_provider(self, provider):
        source = _color_bgr()
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["colorization"]["requested"])
        self.assertFalse(parameters["colorization"]["applied"])
        provider.assert_not_called()

    @override_settings(
        COLORIZATION_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.colorize_with_siggraph17")
    def test_enabled_colorization_of_a_grayscale_source_is_auditable(self, provider):
        source = _grayscale_bgr()
        colorized = _color_bgr(*source.shape[:2])
        provider.return_value = (colorized, {"provider": "colorization_siggraph17"})
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertTrue(parameters["colorization"]["requested"])
        self.assertTrue(parameters["colorization"]["applied"])
        self.assertIn("colorization_siggraph17", parameters["algorithm"])
        provider.assert_called_once()

    @override_settings(
        COLORIZATION_ENABLED=True, HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False, AI_RESTORATION_ENABLED=False,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.colorize_with_siggraph17")
    def test_provider_failure_preserves_pipeline_output(self, provider):
        provider.side_effect = ColorizationUnavailable("Colorization inference failed safely.")
        source = _grayscale_bgr()
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["colorization"]["applied"])
        self.assertEqual(
            parameters["colorization"]["fallback_reason"],
            "Colorization inference failed safely.",
        )

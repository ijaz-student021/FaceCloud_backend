import hashlib
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from .gfpgan import (
    GFPGANUnavailable,
    _load_restorer,
    clear_model_cache,
    restore_faces,
)
from .services import analyze_image_quality, enhance_bgr, restore_degraded_faces


class GFPGANAdapterTests(SimpleTestCase):
    def tearDown(self):
        clear_model_cache()

    @override_settings(
        GFPGAN_MODEL_PATH="missing-private-model.pth",
        GFPGAN_MODEL_SHA256="0" * 64,
    )
    def test_missing_private_checkpoint_fails_without_downloading(self):
        with self.assertRaisesRegex(
            GFPGANUnavailable,
            "checkpoint is unavailable",
        ):
            _load_restorer()

    def test_checksum_is_verified_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "GFPGANv1.4.pth"
            model_path.write_bytes(b"not-the-licensed-checkpoint")
            with (
                override_settings(
                    GFPGAN_MODEL_PATH=str(model_path),
                    GFPGAN_MODEL_SHA256="0" * 64,
                ),
                patch("enhancement.gfpgan._import_runtime") as runtime_import,
            ):
                with self.assertRaisesRegex(
                    GFPGANUnavailable,
                    "integrity verification",
                ):
                    _load_restorer()
            runtime_import.assert_not_called()

    def test_valid_checksum_builds_clean_v14_runtime_without_background_upscale(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "GFPGANv1.4.pth"
            payload = b"test-checkpoint"
            model_path.write_bytes(payload)
            expected_hash = hashlib.sha256(payload).hexdigest()
            restorer_class = Mock()
            restorer_instance = Mock()
            restorer_class.return_value = restorer_instance
            torch = Mock()
            torch.cuda.is_available.return_value = False
            torch.device.side_effect = lambda name: name
            with (
                override_settings(
                    GFPGAN_MODEL_PATH=str(model_path),
                    GFPGAN_MODEL_SHA256=expected_hash,
                    GFPGAN_ARCH="clean",
                    GFPGAN_CHANNEL_MULTIPLIER=2,
                    GFPGAN_DEVICE="auto",
                ),
                patch(
                    "enhancement.gfpgan._import_runtime",
                    return_value=(torch, restorer_class),
                ),
            ):
                loaded = _load_restorer()

            self.assertIs(loaded, restorer_instance)
            restorer_class.assert_called_once_with(
                model_path=str(model_path.resolve()),
                upscale=1,
                arch="clean",
                channel_multiplier=2,
                bg_upsampler=None,
                device="cpu",
            )

    @override_settings(
        GFPGAN_MAX_INPUT_PIXELS=1_000_000,
        GFPGAN_MAX_FACES=5,
        GFPGAN_RESTORATION_WEIGHT=0.35,
        GFPGAN_MODEL_SHA256="a" * 64,
        GFPGAN_ARCH="clean",
    )
    @patch("enhancement.gfpgan._load_restorer")
    def test_provider_rejects_dimension_changes(self, load_restorer):
        source = np.full((64, 96, 3), 80, dtype=np.uint8)
        restorer = Mock()
        restorer.enhance.return_value = (
            [],
            [np.full((32, 32, 3), 90, dtype=np.uint8)],
            np.full((128, 192, 3), 90, dtype=np.uint8),
        )
        load_restorer.return_value = restorer

        with self.assertRaisesRegex(GFPGANUnavailable, "dimensions"):
            restore_faces(source)


class GFPGANPipelineTests(SimpleTestCase):
    @override_settings(GFPGAN_ENABLED=False)
    @patch("enhancement.services.restore_faces")
    def test_disabled_rollout_never_calls_private_provider(self, provider):
        source = np.full((96, 96, 3), 80, dtype=np.uint8)

        restored, metadata = restore_degraded_faces(
            source,
            analyze_image_quality(source),
        )

        self.assertIs(restored, source)
        self.assertTrue(metadata["requested"])
        self.assertFalse(metadata["applied"])
        self.assertIn("disabled", metadata["fallback_reason"])
        provider.assert_not_called()

    @override_settings(
        GFPGAN_ENABLED=True,
        GFPGAN_OUTPUT_BLEND=0.40,
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=False,
    )
    @patch("enhancement.services.restore_faces")
    def test_eligible_face_restoration_is_bounded_and_auditable(self, provider):
        source = np.full((96, 96, 3), 50, dtype=np.uint8)
        provider.return_value = (
            np.full_like(source, 150),
            {
                "provider": "private_gfpgan_v1_4",
                "faces_restored": 1,
                "dimensions_preserved": True,
            },
        )

        enhanced, _level, parameters = enhance_bgr(source, "auto")

        restoration = parameters["face_restoration"]
        self.assertTrue(restoration["applied"])
        self.assertEqual(restoration["faces_restored"], 1)
        self.assertEqual(restoration["output_blend"], 0.40)
        self.assertEqual(enhanced.shape, source.shape)
        self.assertIn("private_gfpgan_v1_4", parameters["algorithm"])
        self.assertIn("face_restoration", parameters["stage_order"])
        self.assertLess(float(enhanced.mean()), 150.0)

    @override_settings(
        GFPGAN_ENABLED=True,
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
    )
    @patch("enhancement.services.restore_faces")
    def test_provider_failure_preserves_pipeline_output(self, provider):
        provider.side_effect = GFPGANUnavailable("GFPGAN inference failed safely.")
        source = np.full((96, 96, 3), 80, dtype=np.uint8)

        enhanced, _level, parameters = enhance_bgr(source, "auto")

        self.assertEqual(enhanced.shape, source.shape)
        self.assertFalse(parameters["face_restoration"]["applied"])
        self.assertEqual(
            parameters["face_restoration"]["fallback_reason"],
            "GFPGAN inference failed safely.",
        )

    @override_settings(
        GFPGAN_ENABLED=True,
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
    )
    @patch("enhancement.services.restore_faces")
    def test_well_lit_sharp_large_image_skips_gfpgan(self, provider):
        source = np.full((800, 800, 3), 150, dtype=np.uint8)
        source[::2, ::2] = 110
        source[1::2, 1::2] = 190

        _enhanced, _level, parameters = enhance_bgr(source, "auto")

        self.assertFalse(parameters["face_restoration"]["requested"])
        self.assertFalse(parameters["face_restoration"]["applied"])
        provider.assert_not_called()

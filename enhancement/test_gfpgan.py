import hashlib
import io
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from .gfpgan import (
    GFPGANUnavailable,
    _load_restorer,
    _numpy_nms,
    clear_model_cache,
    gfpgan_healthcheck,
    restore_faces,
)
from .services import analyze_image_quality, enhance_bgr, restore_degraded_faces


class GFPGANAdapterTests(SimpleTestCase):
    def tearDown(self):
        clear_model_cache()

    @staticmethod
    def _auxiliary_settings(directory):
        root = Path(directory)
        weights = root / "gfpgan" / "weights"
        weights.mkdir(parents=True)
        detector = weights / "detection_Resnet50_Final.pth"
        parser = weights / "parsing_parsenet.pth"
        detector_payload = b"test-detector"
        parser_payload = b"test-parser"
        detector.write_bytes(detector_payload)
        parser.write_bytes(parser_payload)
        return {
            "BASE_DIR": root,
            "GFPGAN_DETECTION_MODEL_PATH": detector,
            "GFPGAN_DETECTION_MODEL_SHA256": hashlib.sha256(
                detector_payload
            ).hexdigest(),
            "GFPGAN_PARSING_MODEL_PATH": parser,
            "GFPGAN_PARSING_MODEL_SHA256": hashlib.sha256(
                parser_payload
            ).hexdigest(),
        }

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

    def test_healthcheck_reports_safe_checkpoint_metadata_without_loading_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "GFPGANv1.4.pth"
            payload = b"licensed-test-checkpoint"
            model_path.write_bytes(payload)
            expected_hash = hashlib.sha256(payload).hexdigest()
            auxiliary_settings = self._auxiliary_settings(directory)
            with (
                override_settings(
                    GFPGAN_MODEL_PATH=str(model_path),
                    GFPGAN_MODEL_SHA256=expected_hash,
                    **auxiliary_settings,
                ),
                patch("enhancement.gfpgan._load_restorer") as load_restorer,
            ):
                metadata = gfpgan_healthcheck(load_model=False)

            self.assertEqual(metadata["checkpoint"], "GFPGANv1.4.pth")
            self.assertEqual(
                metadata["checkpoint_sha256_prefix"],
                expected_hash[:12],
            )
            self.assertFalse(metadata["runtime_loaded"])
            load_restorer.assert_not_called()

    def test_management_preflight_validates_checkpoint_without_exposing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "GFPGANv1.4.pth"
            payload = b"licensed-test-checkpoint"
            model_path.write_bytes(payload)
            expected_hash = hashlib.sha256(payload).hexdigest()
            auxiliary_settings = self._auxiliary_settings(directory)
            output = io.StringIO()
            with override_settings(
                GFPGAN_MODEL_PATH=str(model_path),
                GFPGAN_MODEL_SHA256=expected_hash,
                **auxiliary_settings,
            ):
                call_command("check_gfpgan", stdout=output)

            rendered = output.getvalue()
            self.assertIn("GFPGAN preflight passed.", rendered)
            self.assertIn('"runtime_loaded": false', rendered)
            self.assertNotIn(str(model_path.parent), rendered)

    def test_valid_checksum_builds_clean_v14_runtime_without_background_upscale(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "GFPGANv1.4.pth"
            payload = b"test-checkpoint"
            model_path.write_bytes(payload)
            expected_hash = hashlib.sha256(payload).hexdigest()
            auxiliary_settings = self._auxiliary_settings(directory)
            restorer_class = Mock()
            restorer_instance = Mock()
            restorer_class.return_value = restorer_instance
            torch = Mock()
            torch.cuda.is_available.return_value = False
            torch.backends.mps.is_available.return_value = False
            torch.device.side_effect = lambda name: name
            with (
                override_settings(
                    GFPGAN_MODEL_PATH=str(model_path),
                    GFPGAN_MODEL_SHA256=expected_hash,
                    GFPGAN_ARCH="clean",
                    GFPGAN_CHANNEL_MULTIPLIER=2,
                    GFPGAN_DEVICE="auto",
                    **auxiliary_settings,
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

    @override_settings(GFPGAN_MAX_INPUT_PIXELS=1_000_000)
    def test_provider_rejects_non_uint8_input(self):
        source = np.zeros((64, 64, 3), dtype=np.float32)
        with self.assertRaisesRegex(GFPGANUnavailable, "input image is invalid"):
            restore_faces(source)

    def test_numpy_nms_fallback_suppresses_overlapping_lower_score_box(self):
        detections = np.array(
            [
                [0, 0, 20, 20, 0.95],
                [1, 1, 21, 21, 0.80],
                [50, 50, 70, 70, 0.70],
            ],
            dtype=np.float32,
        )

        self.assertEqual(_numpy_nms(detections, 0.4), [0, 2])


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
        self.assertLess(restoration["output_blend"], 0.40)
        self.assertTrue(restoration["candidate_guard"]["accepted"])
        self.assertTrue(restoration["candidate_guard"]["blend_reduced"])
        self.assertEqual(enhanced.shape, source.shape)
        self.assertIn("private_gfpgan_v1_4", parameters["algorithm"])
        self.assertIn("face_restoration", parameters["stage_order"])
        self.assertIn("validate_output", parameters["stage_order"])
        self.assertTrue(parameters["resolution_contract"]["validated"])
        self.assertTrue(
            parameters["resolution_contract"][
                "enhanced_image_was_not_downscaled"
            ]
        )
        self.assertLess(float(enhanced.mean()), 150.0)

    @override_settings(
        GFPGAN_ENABLED=True,
        GFPGAN_OUTPUT_BLEND=0.25,
        GFPGAN_MIN_OUTPUT_BLEND=0.08,
    )
    @patch("enhancement.services.restore_faces")
    def test_safe_face_restoration_uses_configured_blend(self, provider):
        source = np.full((96, 96, 3), 80, dtype=np.uint8)
        provider.return_value = (
            np.full_like(source, 88),
            {
                "provider": "private_gfpgan_v1_4",
                "faces_restored": 1,
                "dimensions_preserved": True,
            },
        )

        restored, metadata = restore_degraded_faces(
            source,
            {"defects": ["severe_blur"]},
        )

        self.assertTrue(metadata["applied"])
        self.assertEqual(metadata["output_blend"], 0.25)
        self.assertTrue(metadata["candidate_guard"]["accepted"])
        self.assertFalse(metadata["candidate_guard"]["blend_reduced"])
        self.assertGreater(float(restored.mean()), float(source.mean()))

    @override_settings(
        GFPGAN_ENABLED=True,
        GFPGAN_OUTPUT_BLEND=0.25,
        GFPGAN_MIN_OUTPUT_BLEND=0.20,
    )
    @patch("enhancement.services.restore_faces")
    def test_destructive_candidate_is_rejected_at_minimum_safe_blend(self, provider):
        source = np.full((96, 96, 3), 90, dtype=np.uint8)
        destructive = np.zeros_like(source)
        destructive[:, ::2] = (255, 0, 255)
        destructive[:, 1::2] = (0, 255, 0)
        provider.return_value = (
            destructive,
            {
                "provider": "private_gfpgan_v1_4",
                "faces_restored": 1,
                "dimensions_preserved": True,
            },
        )

        restored, metadata = restore_degraded_faces(
            source,
            {"defects": ["severe_blur"]},
        )

        self.assertFalse(metadata["applied"])
        self.assertFalse(metadata["candidate_guard"]["accepted"])
        self.assertIn("quality limits", metadata["fallback_reason"])
        np.testing.assert_array_equal(restored, source)

    @override_settings(
        GFPGAN_ENABLED=True,
        GFPGAN_ALLOW_MILD_BLUR=False,
    )
    @patch("enhancement.services.restore_faces")
    def test_mild_blur_can_be_excluded_by_rollout_policy(self, provider):
        source = np.full((800, 800, 3), 100, dtype=np.uint8)

        restored, metadata = restore_degraded_faces(
            source,
            {"defects": ["mild_blur"]},
        )

        self.assertIs(restored, source)
        self.assertFalse(metadata["requested"])
        provider.assert_not_called()

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

"""The 2026-10 model set: Retinexformer (low light), NAFNet-SIDD (denoise), CodeFormer (faces).

Adapter behaviour (refusals, checksums, shapes) and how the pipeline chooses between them. Tests that need a
real checkpoint skip when the file or PyTorch is not on this machine; the rest use stand-ins.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import codeformer, lowlight
from .codeformer import CodeFormerUnavailable
from .hd_restoration import HDRestorationUnavailable
from .lowlight import LowLightUnavailable
from .services import enhance_bgr
from .test_balanced_policy import dark_photo, fake_zero_dce
from .test_pipeline_regressions import NO_MODELS, textured

TORCH = importlib.util.find_spec("torch") is not None and importlib.util.find_spec("einops") is not None


def has(path_setting):
    return Path(getattr(settings, path_setting)).is_file()


def learned(**flags):
    return override_settings(**{**NO_MODELS, "ENHANCEMENT_RENDER_POLICY": "learned_v1", **flags})


class RetinexformerAdapterTests(SimpleTestCase):
    @override_settings(RETINEXFORMER_MODEL_PATH="/nonexistent/retinexformer.pth")
    def test_a_missing_checkpoint_is_refused(self):
        with self.assertRaisesRegex(LowLightUnavailable, "unavailable"):
            lowlight.enhance_with_retinexformer(dark_photo())

    @override_settings(RETINEXFORMER_MODEL_SHA256="0" * 64)
    def test_a_checkpoint_that_does_not_match_its_pinned_checksum_is_never_loaded(self):
        if not has("RETINEXFORMER_MODEL_PATH"):
            self.skipTest("The Retinexformer checkpoint is not installed here.")
        with self.assertRaisesRegex(LowLightUnavailable, "integrity"):
            lowlight.enhance_with_retinexformer(dark_photo())

    def test_an_empty_image_is_refused(self):
        with self.assertRaises(LowLightUnavailable):
            lowlight.enhance_with_retinexformer(np.zeros((0, 0, 3), np.uint8))

    @override_settings(RETINEXFORMER_DEVICE="auto")
    def test_the_real_model_brightens_a_dark_photo_and_keeps_its_size(self):
        if not (TORCH and has("RETINEXFORMER_MODEL_PATH")):
            self.skipTest("PyTorch or the Retinexformer checkpoint is not installed here.")
        source = dark_photo(size=(250, 330))  # not a multiple of 4: the adapter pads and crops
        output, metadata = lowlight.enhance_with_retinexformer(source)
        self.assertEqual(output.shape, source.shape)
        self.assertGreater(float(output.mean()), float(source.mean()) + 20)
        self.assertEqual(metadata["provider"], "retinexformer_ntire")


class RetinexformerTileTests(SimpleTestCase):
    """The CPU tile cap: measured to cut memory and time at the same quality (docs/deployment/hostinger-readiness-2026-10.md)."""

    def run_with(self, device_type, **settings_):
        from types import SimpleNamespace

        seen = {}

        def fake_fallback(run, *, device, tile, overlap, label, error_class):
            seen["tile"] = tile
            return dark_photo(), {"device": str(device)}

        with override_settings(RETINEXFORMER_TILE_SIZE=1024, **settings_), \
                patch("enhancement.lowlight._torch", return_value=object()), \
                patch("enhancement.lowlight._load_model", return_value=(object(), SimpleNamespace(type=device_type))), \
                patch("enhancement.lowlight.runtime.run_with_memory_fallback", side_effect=fake_fallback):
            lowlight.enhance_with_retinexformer(dark_photo())
        return seen["tile"]

    def test_a_cpu_uses_the_smaller_tile(self):
        self.assertEqual(self.run_with("cpu"), 512)
        self.assertEqual(self.run_with("cpu", RETINEXFORMER_CPU_TILE_SIZE=256), 256)

    def test_a_gpu_keeps_the_configured_tile(self):
        self.assertEqual(self.run_with("cuda"), 1024)
        self.assertEqual(self.run_with("mps"), 1024)


class DenoiseAdapterTests(SimpleTestCase):
    @override_settings(NAFNET_SIDD_MODEL_PATH="/nonexistent/sidd.pth")
    def test_a_missing_checkpoint_is_refused(self):
        from .hd_restoration import denoise_with_nafnet

        with self.assertRaisesRegex(HDRestorationUnavailable, "unavailable"):
            denoise_with_nafnet(textured())

    def test_the_real_model_reduces_noise_and_keeps_size_and_colour(self):
        if not (TORCH and has("NAFNET_SIDD_MODEL_PATH")):
            self.skipTest("PyTorch or the NAFNet-SIDD checkpoint is not installed here.")
        from .hd_restoration import denoise_with_nafnet

        clean = cv2.GaussianBlur(textured(160, 200, base=120), (0, 0), 2.0)
        noisy = np.clip(clean.astype(np.float32) + np.random.default_rng(4).normal(0, 9, clean.shape), 0, 255).astype(np.uint8)
        output, metadata = denoise_with_nafnet(noisy)
        error = lambda image: float(np.abs(image.astype(np.float32) - clean.astype(np.float32)).mean())  # noqa: E731
        self.assertEqual(output.shape, noisy.shape)
        self.assertLess(error(output), error(noisy))
        self.assertEqual(metadata["provider"], "nafnet_sidd_width64")


class CodeFormerAdapterTests(SimpleTestCase):
    @override_settings(CODEFORMER_MODEL_PATH="/nonexistent/codeformer.pth")
    def test_a_missing_checkpoint_is_refused(self):
        landmarks = np.array([[100, 120], [160, 120], [130, 160], [105, 190], [155, 190]], np.float32)
        with patch.object(codeformer, "_detect", return_value=[((80, 90, 100, 120), landmarks)]):
            with self.assertRaisesRegex(CodeFormerUnavailable, "unavailable"):
                codeformer.restore_faces_codeformer(textured(300, 260))

    def test_an_image_with_no_face_is_refused_not_guessed(self):
        with patch.object(codeformer, "_detect", return_value=[]):
            with self.assertRaisesRegex(CodeFormerUnavailable, "No face"):
                codeformer.restore_faces_codeformer(textured(300, 260))

    def test_fidelity_outside_zero_to_one_is_refused(self):
        with self.assertRaises(CodeFormerUnavailable):
            codeformer.restore_faces_codeformer(textured(300, 260), fidelity=1.5)

    @override_settings(CODEFORMER_MAX_INPUT_PIXELS=100)
    def test_an_oversized_image_is_refused(self):
        with self.assertRaisesRegex(CodeFormerUnavailable, "safety limit"):
            codeformer.restore_faces_codeformer(textured(300, 260))

    def test_the_real_model_restores_in_place_and_keeps_the_image_size(self):
        if not (TORCH and has("CODEFORMER_MODEL_PATH")):
            self.skipTest("PyTorch or the CodeFormer checkpoint is not installed here.")
        source = textured(400, 300, base=140)
        landmarks = np.array([[110, 150], [190, 150], [150, 200], [118, 245], [182, 245]], np.float32)
        with patch.object(codeformer, "_detect", return_value=[((90, 120, 120, 150), landmarks)]):
            output, metadata = codeformer.restore_faces_codeformer(source, fidelity=0.7)
        self.assertEqual(output.shape, source.shape)
        self.assertEqual(metadata["faces_restored"], 1)
        np.testing.assert_array_equal(output[:20, :20], source[:20, :20])  # far from the face: untouched


class PipelineChoosesTheModelTests(SimpleTestCase):
    @learned(RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=True)
    def test_retinexformer_is_preferred_to_zero_dce(self):
        source = dark_photo()
        with patch("enhancement.services.enhance_with_retinexformer",
                   return_value=(cv2.add(source, 60), {"provider": "retinexformer_ntire"})) as retinex, \
                patch("enhancement.services.enhance_with_zero_dce", side_effect=AssertionError("not needed")) as zero:
            _out, _level, parameters = enhance_bgr(source, "high")
        retinex.assert_called_once()
        zero.assert_not_called()
        self.assertEqual(parameters["algorithm"].split("+")[0], "retinexformer_hybrid_v1")
        self.assertTrue(parameters["zero_dce"]["applied"])
        self.assertEqual(parameters["zero_dce"]["provider"], "retinexformer_ntire")

    @learned(RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=True)
    def test_zero_dce_is_the_fallback_when_retinexformer_cannot_run(self):
        source = dark_photo()
        with patch("enhancement.services.enhance_with_retinexformer", side_effect=LowLightUnavailable("checkpoint unavailable")), \
                patch("enhancement.services.enhance_with_zero_dce", side_effect=fake_zero_dce) as zero:
            _out, _level, parameters = enhance_bgr(source, "high")
        zero.assert_called_once()
        self.assertEqual(parameters["algorithm"].split("+")[0], "zero_dce_hybrid_v1")
        self.assertEqual(parameters["zero_dce"]["retinexformer_fallback_reason"], "checkpoint unavailable")

    @learned(RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=False)
    def test_with_no_model_available_the_opencv_correction_runs_and_says_why(self):
        source = dark_photo()
        with patch("enhancement.services.enhance_with_retinexformer", side_effect=LowLightUnavailable("no GPU runtime")):
            out, _level, parameters = enhance_bgr(source, "high")
        self.assertEqual(parameters["algorithm"].split("+")[0], "opencv_adaptive_restoration_router_v2")
        self.assertFalse(parameters["zero_dce"]["applied"])
        self.assertIn("no GPU runtime", parameters["zero_dce"]["fallback_reason"])
        self.assertGreater(float(out.mean()), float(source.mean()))

    @override_settings(**{**NO_MODELS, "ENHANCEMENT_RENDER_POLICY": "balanced_v1", "RETINEXFORMER_ENABLED": True})
    def test_the_balanced_policy_never_runs_retinexformer_and_automatic_mode_withholds_the_lift(self):
        with patch("enhancement.services.enhance_with_retinexformer", side_effect=AssertionError("must not run")) as retinex:
            enhance_bgr(dark_photo(), "high")
            enhance_bgr(dark_photo(), "auto")
        retinex.assert_not_called()

    @learned(NAFNET_DENOISE_ENABLED=True, ADAPTIVE_DENOISE_ENABLED=True)
    def test_a_denoise_model_failure_falls_back_to_the_classical_denoise(self):
        noisy = np.clip(textured(240, 320, base=110).astype(np.float32) + np.random.default_rng(6).normal(0, 14, (240, 320, 3)), 0, 255).astype(np.uint8)
        with patch("enhancement.services.denoise_with_nafnet", side_effect=HDRestorationUnavailable("model not loaded")):
            _out, _level, parameters = enhance_bgr(noisy, "high")
        denoise = parameters["adaptive_denoise"]
        self.assertNotEqual(denoise.get("method"), "nafnet_sidd_width64")
        self.assertEqual(denoise.get("learned_fallback_reason", "model not loaded"), "model not loaded")

    @learned(CODEFORMER_ENABLED=True)
    def test_codeformer_is_the_face_restorer_when_it_is_on(self):
        from .test_blur_measure import blurred_photo

        source = blurred_photo(480, 640)
        with patch("enhancement.services._face_restoration_gate", return_value=""), \
                patch("enhancement.services.restore_faces_codeformer",
                   side_effect=lambda image: (image, {"provider": "codeformer_v0_1", "faces_restored": 0})) as codeformer_call, \
                patch("enhancement.services.restore_faces", side_effect=AssertionError("GFPGAN must not run")) as gfpgan:
            _out, _level, parameters = enhance_bgr(source, "auto")
        codeformer_call.assert_called_once()
        gfpgan.assert_not_called()
        self.assertEqual(parameters["face_restoration"]["provider"], "codeformer_v0_1")
        self.assertTrue(parameters["feature_flags"]["codeformer_enabled"])

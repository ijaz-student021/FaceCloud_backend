"""Zero-DCE is loaded only if its file matches the pinned checksum.

Mocked: OpenCV's ONNX loader is replaced, so these tests prove the integrity
check, not inference. Real inference is checked by scripts/validate_models.py.
"""
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from . import zero_dce
from .zero_dce import ZeroDCEUnavailable, _load_zero_dce_model, enhance_with_zero_dce


class ZeroDCEIntegrityTests(SimpleTestCase):
    def setUp(self):
        zero_dce._MODEL_CACHE.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "zero_dce.onnx"
        self.path.write_bytes(b"pretend onnx bytes")
        self.digest = hashlib.sha256(b"pretend onnx bytes").hexdigest()

    def test_a_matching_file_is_loaded(self):
        with override_settings(ZERO_DCE_MODEL_SHA256=self.digest), \
                patch("enhancement.zero_dce.cv2.dnn.readNetFromONNX", return_value="network") as reader:
            self.assertEqual(_load_zero_dce_model(self.path), "network")
        reader.assert_called_once()

    def test_a_file_that_runs_but_is_not_the_pinned_one_is_never_loaded(self):
        with override_settings(ZERO_DCE_MODEL_SHA256="0" * 64), \
                patch("enhancement.zero_dce.cv2.dnn.readNetFromONNX") as reader:
            with self.assertRaisesRegex(ZeroDCEUnavailable, "integrity verification"):
                _load_zero_dce_model(self.path)
        reader.assert_not_called()

    def test_no_configured_checksum_means_no_load(self):
        for value in ("", "abc"):
            with override_settings(ZERO_DCE_MODEL_SHA256=value), \
                    patch("enhancement.zero_dce.cv2.dnn.readNetFromONNX") as reader:
                with self.assertRaisesRegex(ZeroDCEUnavailable, "checksum is not configured"):
                    _load_zero_dce_model(self.path)
            reader.assert_not_called()

    def test_the_pipeline_falls_back_to_the_opencv_correction(self):
        from .services import enhance_bgr

        dark = np.full((96, 128, 3), 30, dtype=np.uint8)
        with override_settings(
            ZERO_DCE_ENABLED=True, ZERO_DCE_MODEL_PATH=str(self.path), ZERO_DCE_MODEL_SHA256="0" * 64,
            IDENTITY_GUARD_ENABLED=False,
        ENHANCEMENT_RENDER_POLICY="learned_v1"):
            _output, _level, parameters = enhance_bgr(dark, "auto")
        self.assertFalse(parameters["zero_dce"]["applied"])
        self.assertIn("integrity verification", parameters["zero_dce"]["fallback_reason"])

    def test_the_default_pin_is_the_manifest_entry(self):
        import json

        from django.conf import settings

        manifest = json.loads((Path(settings.BASE_DIR) / "docs" / "gpu" / "model_manifest.json").read_text())
        self.assertEqual(settings.ZERO_DCE_MODEL_SHA256, manifest["roots"]["models"]["zero_dce_epoch99.onnx"]["sha256"])

    def test_enhance_reports_an_unavailable_model_as_such(self):
        with override_settings(ZERO_DCE_MODEL_PATH=str(self.path), ZERO_DCE_MODEL_SHA256="0" * 64):
            with self.assertRaises(ZeroDCEUnavailable):
                enhance_with_zero_dce(np.zeros((32, 32, 3), dtype=np.uint8))

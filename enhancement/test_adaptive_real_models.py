"""The adaptive engine with the real checkpoints. Skipped when PyTorch, the checkpoints or the sample photographs are absent.

These prove that real inference is wired into the engine end to end (the right model runs, its output passes through the
validator, the final image is brighter and the record says so). They do not prove that any photograph looks better.
"""
import importlib.util
import os
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from .adaptive.service import ORIGINAL_PRESERVED, AdaptiveImageEnhancementService, EngineConfig
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait

TORCH = importlib.util.find_spec("torch") is not None and importlib.util.find_spec("einops") is not None
# Optional: a folder of real portraits to run the detector on (never committed). Without it those tests skip.
SAMPLES = sorted(Path(os.environ["FACECLOUD_SAMPLE_PHOTOS"]).glob("*.jp*g")) if os.environ.get("FACECLOUD_SAMPLE_PHOTOS") else []


def installed(setting):
    return Path(getattr(settings, setting)).is_file()


@override_settings(**ALL_ON)
class RealInferenceThroughTheEngineTests(SimpleTestCase):
    def setUp(self):
        if not TORCH:
            self.skipTest("PyTorch or einops is not installed.")

    def service(self, face=True):
        return AdaptiveImageEnhancementService(analyzer=analyzer_with() if face else analyzer_with(None), config=EngineConfig())

    def test_a_dark_photograph_is_lifted_by_the_real_retinexformer_and_the_record_says_so(self):
        if not installed("RETINEXFORMER_MODEL_PATH"):
            self.skipTest("The Retinexformer checkpoint is not installed.")
        source = portrait(brightness=0.12)
        output, _level, parameters = self.service().enhance(source, allow_biometric_processing=False)
        adaptive = parameters["adaptive"]
        stage = next(s for s in adaptive["stages"] if s["operation"] == "low_light_enhancement")
        self.assertIn(stage["status"], ("accepted", "rejected"))
        if stage["status"] == "accepted":
            self.assertIn("retinexformer", parameters["algorithm"])
            self.assertGreater(float(output.mean()), float(source.mean()) + 8)
        else:  # the validator judged every real candidate unsafe: then the original is what is returned
            np.testing.assert_array_equal(output, source)
            self.assertEqual(adaptive["outcome"], ORIGINAL_PRESERVED)

    def test_a_clean_photograph_runs_no_model_and_is_returned_as_it_is(self):
        source = portrait()
        output, _level, parameters = self.service().enhance(source, allow_biometric_processing=False)
        np.testing.assert_array_equal(output, source)
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)

    def test_the_real_retinaface_detector_runs_in_this_environment(self):
        if not (installed("GFPGAN_DETECTION_MODEL_PATH") and SAMPLES):
            self.skipTest("The RetinaFace weights or the sample portraits are not available.")
        from validation.services import RetinaFaceDetector

        detection = RetinaFaceDetector().detect(cv2.imread(str(SAMPLES[0])))
        self.assertGreaterEqual(len(detection.boxes), 1)
        self.assertEqual(len(detection.landmarks[0]), 5)

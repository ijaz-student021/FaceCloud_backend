"""The restoration chain with the real checkpoints on a real photograph. Skipped without PyTorch, the checkpoints, YuNet or sample photographs.

    FACECLOUD_SAMPLE_PHOTOS=~/Desktop/images FACE_DETECTOR_PROVIDER=yunet_v1 YUNET_MODEL_SHA256=... python manage.py test enhancement.test_restoration_real_models

What is asserted is the contract, not a look: when a stage was accepted the face's own detail rose by at least the gate's factor (deblur) or the
output is larger (upscale); when it was not accepted the result says why. Numbers per photograph are in scripts/evaluate_restoration.py.
"""
import importlib.util
import os
from pathlib import Path

import cv2
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import restoration
from .adaptive.service import AdaptiveImageEnhancementService

TORCH = importlib.util.find_spec("torch") is not None and importlib.util.find_spec("einops") is not None
SAMPLES = sorted(Path(os.environ["FACECLOUD_SAMPLE_PHOTOS"]).expanduser().glob("*.jp*g")) if os.environ.get("FACECLOUD_SAMPLE_PHOTOS") else []


def installed(setting):
    return Path(getattr(settings, setting)).is_file()


@override_settings(ENHANCEMENT_RENDER_POLICY="adaptive_v1", NAFNET_DEBLUR_ENABLED=True, HD_RESTORATION_ENABLED=True,
                   CODEFORMER_ENABLED=False, GFPGAN_ENABLED=False, ADAPTIVE_DEBUG_ARTIFACTS=False)
class RestorationRealModelTests(SimpleTestCase):
    def setUp(self):
        if not (TORCH and SAMPLES and installed("NAFNET_MODEL_PATH") and installed("REALESRGAN_MODEL_PATH") and installed("YUNET_MODEL_PATH")):
            self.skipTest("PyTorch, the NAFNet and Real-ESRGAN checkpoints, YuNet or FACECLOUD_SAMPLE_PHOTOS are not available.")
        photo = cv2.imread(str(SAMPLES[0]))
        scale = 960 / max(photo.shape[:2])
        self.clean = cv2.resize(photo, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        self.service = AdaptiveImageEnhancementService()
        detail = self.service._analyzer().analyze(self.clean).main_face
        if detail is None:
            self.skipTest("No face was detected in the first sample photograph.")

    def test_a_strongly_blurred_face_is_deblurred_or_the_result_says_why_not(self):
        blurred = cv2.GaussianBlur(self.clean, (0, 0), 7.0)
        before = self.service._analyzer().analyze(blurred).face_blur
        self.assertEqual(before["status"], "high")
        output, _level, parameters = self.service.enhance(blurred, allow_biometric_processing=False)
        stages = restoration.describe_stages(parameters)
        if any(stage["stage"] == "deblur" for stage in stages["applied_stages"]):
            after = self.service._analyzer().analyze(output).face_blur
            self.assertGreaterEqual(after["detail"], before["detail"] * 1.25)
        else:
            self.assertTrue(stages["hd_unavailable_reason"] or stages["skipped_stages"], "an unrestored blurred face must say why")

    def test_a_low_resolution_photograph_is_upscaled_or_the_result_says_why_not(self):
        scale = 240.0 / min(self.clean.shape[:2])
        small = cv2.resize(self.clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        output, _level, parameters = self.service.enhance(small, allow_biometric_processing=False)
        stages = restoration.describe_stages(parameters)
        upscale = [stage for stage in stages["applied_stages"] if stage["stage"] == "upscale"]
        if upscale:
            self.assertGreater(output.shape[1], small.shape[1])
            self.assertEqual(upscale[0]["output_size"], [output.shape[1], output.shape[0]])
        else:
            self.assertTrue(stages["hd_unavailable_reason"] or stages["skipped_stages"])

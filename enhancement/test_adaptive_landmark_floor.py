"""A tiny face's landmark jitter is detector noise, not a changed face (ADAPTIVE_LANDMARK_PIXEL_FLOOR).

Found on a real 480x270 rooftop photograph: the face-shaped silhouette was 21 px wide, Real-ESRGAN's candidate passed every gate
(data consistency 49.6 dB, detail 1.25x bicubic) and was refused for ``face_geometry_changed`` at 0.078 = 1.6 px.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive.analyzer import FaceObservation
from .adaptive.validator import FinalImageQualityValidator, Limits

TINY = (278, 59, 21, 28)       # the silhouette: 21 px wide
LARGE = (150, 100, 180, 220)


def face(box, scale=1, dx=0.0):
    scaled = tuple(int(v * scale) for v in box)
    landmarks = [(x * scale + dx, y * scale) for x, y in base.landmarks_for(box)]
    return FaceObservation(box=scaled, confidence=0.9, landmarks=landmarks, area_ratio=0.01, short_edge=scaled[2],
                           luminance={}, detail=80.0, visibility="good", quality="good")


def gate(operation, box, dx_original_px, identity=None, scale=2):
    """The identity gate for a candidate ``scale`` times larger whose landmarks moved ``dx_original_px`` pixels of the ORIGINAL photograph."""
    original = SimpleNamespace(faces=[face(box)], width=640)
    candidate = SimpleNamespace(faces=[face(box, scale, dx=dx_original_px * scale)], width=640 * scale)
    verdict = {"reasons": [], "gates": {}}
    FinalImageQualityValidator(None, original, identity=identity)._identity_gate(verdict, None, candidate, operation)
    return verdict["gates"]["identity"]


VERIFIED = SimpleNamespace(check=lambda _image, restoring=False: {"status": "verified", "similarity": 0.9, "threshold": 0.4})


class LandmarkPixelFloorTests(SimpleTestCase):
    def test_the_default_is_two_pixels_and_is_a_setting(self):
        self.assertEqual(Limits().landmark_pixel_floor, 2.0)
        with override_settings(ADAPTIVE_LANDMARK_PIXEL_FLOOR=0.5):
            self.assertEqual(Limits().landmark_pixel_floor, 0.5)

    def test_a_pixel_and_a_half_of_jitter_on_a_21_px_face_is_noise_for_an_upscale(self):
        status = gate("super_resolution", TINY, 1.6)
        self.assertTrue(status["passed"], status)
        self.assertAlmostEqual(status["landmark_shift"], 1.6 / 21, places=3)       # 0.076: over the 0.06 share limit, under 2 px

    @override_settings(ADAPTIVE_LANDMARK_PIXEL_FLOOR=0)
    def test_with_the_floor_off_the_same_jitter_is_still_refused_as_before(self):
        self.assertFalse(gate("super_resolution", TINY, 1.6)["passed"])

    def test_the_other_non_generative_operations_get_the_floor_too(self):
        for operation in ("denoising", "low_light_enhancement", "motion_deblurring"):
            self.assertTrue(gate(operation, TINY, 1.6, scale=1)["passed"], operation)

    def test_three_pixels_on_a_tiny_face_is_a_real_move_and_is_refused(self):
        self.assertFalse(gate("super_resolution", TINY, 3.0)["passed"])
        self.assertFalse(gate("denoising", TINY, 3.0, scale=1)["passed"])

    def test_a_large_face_keeps_its_share_limit(self):
        # 5 percent of 180 px = 9 px: the floor (2 px = 1.1 percent) is far under the share limit and changes nothing
        self.assertFalse(gate("denoising", LARGE, 9.0, scale=1)["passed"])
        self.assertTrue(gate("denoising", LARGE, 3.0, scale=1)["passed"])

    def test_face_restoration_is_never_relaxed(self):
        self.assertFalse(gate("face_restoration", TINY, 1.6, identity=VERIFIED)["passed"])

    def test_polish_keeps_its_own_stricter_limit(self):
        self.assertFalse(gate("polish", TINY, 1.6, scale=1)["passed"])

    def test_a_face_that_is_no_longer_found_is_still_a_loss(self):
        original = SimpleNamespace(faces=[face(TINY)], width=640)
        verdict = {"reasons": [], "gates": {}}
        FinalImageQualityValidator(None, original, identity=None)._identity_gate(verdict, None, SimpleNamespace(faces=[], width=1280), "super_resolution")
        self.assertFalse(verdict["gates"]["identity"]["passed"])
        self.assertIn("face_lost", verdict["reasons"])


class PolishFaceStructureTests(SimpleTestCase):
    """Polish moves no pixel; when the detector places the landmarks elsewhere on a soft face, the face's own structure decides (SSIM of the padded face crop)."""

    def setUp(self):
        import cv2
        import numpy as np

        self.cv2, self.np = cv2, np
        self.parent = base.portrait()
        self.box = (150, 100, 180, 220)
        self.original = SimpleNamespace(faces=[face(self.box)], width=640)

    def gate(self, candidate_image, landmark_dx):
        candidate_report = SimpleNamespace(faces=[face(self.box, 1, dx=landmark_dx)], width=640)
        verdict = {"reasons": [], "gates": {}}
        FinalImageQualityValidator(None, self.original, identity=None)._identity_gate(verdict, candidate_image, candidate_report, "polish", self.parent)
        return verdict["gates"]["identity"], verdict["reasons"]

    def polished(self):
        lab = self.cv2.cvtColor(self.parent.astype(self.np.float32) / 255.0, self.cv2.COLOR_BGR2Lab)
        lab[:, :, 0] = self.np.clip(lab[:, :, 0] * 1.04 + 1.0, 0, 100)
        return self.np.clip(self.np.rint(self.cv2.cvtColor(lab, self.cv2.COLOR_Lab2BGR) * 255.0), 0, 255).astype(self.np.uint8)

    def test_a_polished_face_passes_although_the_detector_moved_its_landmarks(self):
        status, reasons = self.gate(self.polished(), landmark_dx=0.05 * self.box[2])       # 5 percent: over polish's 0.02
        self.assertTrue(status["passed"], status)
        self.assertGreaterEqual(status["face_structure_ssim"], 0.985)
        self.assertEqual(reasons, [])

    def test_a_face_that_really_moved_is_still_refused(self):
        moved = self.cv2.warpAffine(self.parent, self.np.float32([[1, 0, 6], [0, 1, 0]]), (self.parent.shape[1], self.parent.shape[0]), borderMode=self.cv2.BORDER_REPLICATE)
        status, reasons = self.gate(moved, landmark_dx=0.05 * self.box[2])
        self.assertFalse(status["passed"], status)
        self.assertLess(status["face_structure_ssim"], 0.985)
        self.assertIn("face_geometry_changed", reasons)

    def test_without_the_pixels_the_old_landmark_rule_stands(self):
        verdict = {"reasons": [], "gates": {}}
        candidate_report = SimpleNamespace(faces=[face(self.box, 1, dx=0.05 * self.box[2])], width=640)
        FinalImageQualityValidator(None, self.original, identity=None)._identity_gate(verdict, None, candidate_report, "polish")
        self.assertFalse(verdict["gates"]["identity"]["passed"])

    def test_only_polish_gets_this_second_opinion(self):
        verdict = {"reasons": [], "gates": {}}
        candidate_report = SimpleNamespace(faces=[face(self.box, 1, dx=0.05 * self.box[2])], width=640)
        FinalImageQualityValidator(None, self.original, identity=None)._identity_gate(verdict, self.polished(), candidate_report, "denoising", self.parent)
        self.assertFalse(verdict["gates"]["identity"]["passed"])

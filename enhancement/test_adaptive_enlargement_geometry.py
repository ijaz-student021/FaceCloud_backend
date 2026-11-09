"""The detector's own placement of an enlarged small face is not a changed face: a candidate the landmark share limit refused is accepted when its landmarks stay within
ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX of the landmarks the detector finds on a PLAIN enlargement of the original to the same size (validator._shift_beyond_plain_enlargement).

Measured with the real models (docs/evaluation/2026-11-05-small-face-geometry.md): on a 114 px face of the library a plain bicubic x2 moved the five landmarks by 0.078 of the face
width and Real-ESRGAN's candidate by 0.080, over the 0.06 limit, so the enlargement of the photograph was refused for ``face_geometry_changed``; against the plain enlargement the
model moved them by 0.030. Here the detector is a stand-in (CI has no weights), so these tests are about the decision rule, not about the detector.
"""
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from validation.services import FaceDetection

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator, Limits
from .test_adaptive_landmark_floor import VERIFIED, face
from .test_adaptive_polish import _no_identity

BOX = (230, 100, 114, 179)             # the library photograph's face: 114 px wide, quality "poor"
SHARE = 114.0                          # 1 px of the original = 1 / 114 of the face width


class Analyzer:
    """What the validator asks for the plain enlargement: the faces the detector finds on it. Records what it was asked."""

    def __init__(self, faces):
        self.faces, self.calls = faces, []

    def analyze(self, image):
        self.calls.append(image.shape[:2])
        return SimpleNamespace(faces=self.faces)


def gate(operation, candidate_dx, plain_dx, scale=2, identity=None, with_original=True, with_analyzer=True, plain_faces=None, candidate_faces=None):
    """The identity gate for a candidate ``scale`` times larger whose landmarks the detector put ``candidate_dx`` pixels (of the ORIGINAL photograph) from where the original's are, when it
    puts them ``plain_dx`` pixels away on a plain enlargement of the same photograph. Returns (gate status, reasons, analyzer)."""
    original_report = SimpleNamespace(faces=[face(BOX, quality="poor")], width=640)
    candidate_report = SimpleNamespace(faces=[face(BOX, scale, dx=candidate_dx * scale)] if candidate_faces is None else candidate_faces, width=640 * scale)
    analyzer = Analyzer([face(BOX, scale, dx=plain_dx * scale)] if plain_faces is None else plain_faces)
    validator = FinalImageQualityValidator(analyzer if with_analyzer else None, original_report, identity=identity, original=np.zeros((480, 640, 3), np.uint8) if with_original else None)
    verdict = {"reasons": [], "gates": {}}
    validator._identity_gate(verdict, np.zeros((480 * scale, 640 * scale, 3), np.uint8), candidate_report, operation)
    return verdict["gates"]["identity"], verdict["reasons"], analyzer


NOISE = 9.1        # 0.0798 of the face width: what the detector measured on the library photograph for a model-free enlargement (0.078) and for Real-ESRGAN (0.080)


class SettingTests(SimpleTestCase):
    def test_the_default_and_the_setting(self):
        self.assertEqual(settings.ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX, 0.035)
        self.assertEqual(Limits().landmark_enlargement_shift_max, 0.035)
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0.02):
            self.assertEqual(Limits().landmark_enlargement_shift_max, 0.02)


class EnlargementGeometryGateTests(SimpleTestCase):
    def test_the_premise_without_the_rule_detector_noise_alone_refuses_the_enlargement(self):
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0):
            status, reasons, _ = gate("super_resolution", NOISE, NOISE)
        self.assertFalse(status["passed"])
        self.assertAlmostEqual(status["landmark_shift"], NOISE / SHARE, places=3)         # 0.080: over the 0.06 limit although nothing but the detector differs
        self.assertIn("face_geometry_changed", reasons)

    def test_detector_noise_alone_does_not_refuse_an_enlargement(self):
        status, reasons, analyzer = gate("super_resolution", NOISE, NOISE)
        self.assertTrue(status["passed"], status)
        self.assertEqual(reasons, [])
        self.assertAlmostEqual(status["landmark_shift"], NOISE / SHARE, places=3)         # the old measurement is still recorded
        self.assertEqual(status["landmark_shift_beyond_plain_enlargement"], 0.0)
        self.assertEqual(len(analyzer.calls), 1)

    def test_a_small_move_by_the_model_beyond_the_noise_is_accepted(self):
        """The library photograph: the model moved the landmarks 0.030 beyond the plain enlargement (3.4 px of 114)."""
        status, _, _ = gate("super_resolution", NOISE + 3.4, NOISE)
        self.assertTrue(status["passed"], status)
        self.assertAlmostEqual(status["landmark_shift_beyond_plain_enlargement"], 3.4 / SHARE, places=3)

    def test_a_real_move_beyond_the_plain_enlargement_is_still_refused(self):
        status, reasons, _ = gate("super_resolution", NOISE + 6.0, NOISE)
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertAlmostEqual(status["landmark_shift_beyond_plain_enlargement"], 6.0 / SHARE, places=3)       # 0.053: over 0.035

    def test_the_limit_is_where_the_setting_says(self):
        limit_px = 0.035 * SHARE                                  # 3.99 px
        self.assertTrue(gate("super_resolution", NOISE + limit_px - 0.3, NOISE)[0]["passed"])
        self.assertFalse(gate("super_resolution", NOISE + limit_px + 0.3, NOISE)[0]["passed"])
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0.05):
            self.assertTrue(gate("super_resolution", NOISE + 5.0, NOISE)[0]["passed"])

    def test_it_is_never_stricter_than_before(self):
        """A candidate the share limit accepts is accepted without a second opinion, even when the plain enlargement happens to be far away; the plain enlargement is not even analysed."""
        status, reasons, analyzer = gate("super_resolution", 2.0, 20.0)
        self.assertTrue(status["passed"])
        self.assertEqual(reasons, [])
        self.assertNotIn("landmark_shift_beyond_plain_enlargement", status)
        self.assertEqual(analyzer.calls, [])

    def test_the_plain_enlargement_is_analysed_once_per_size(self):
        original_report = SimpleNamespace(faces=[face(BOX, quality="poor")], width=640)
        analyzer = Analyzer([face(BOX, 2, dx=NOISE * 2)])
        validator = FinalImageQualityValidator(analyzer, original_report, original=np.zeros((480, 640, 3), np.uint8))
        for extra in (0.0, 1.0, 2.0):
            verdict = {"reasons": [], "gates": {}}
            validator._identity_gate(verdict, np.zeros((960, 1280, 3), np.uint8), SimpleNamespace(faces=[face(BOX, 2, dx=(NOISE + extra) * 2)], width=1280), "super_resolution")
            self.assertTrue(verdict["gates"]["identity"]["passed"])
        self.assertEqual(analyzer.calls, [(960, 1280)])

    def test_the_plain_enlargement_is_the_size_of_the_candidate(self):
        _, _, analyzer = gate("super_resolution", NOISE, NOISE, scale=3)
        self.assertEqual(analyzer.calls, [(1440, 1920)])

    def test_the_other_non_generative_operations_after_an_enlargement_get_it_too(self):
        """After an accepted enlargement every later step is judged on the enlarged grid against the original's landmarks, so it inherits the detector's placement (the 127 px photograph's final layer
        was refused at 0.050 against 0.04 although it moved nothing)."""
        for operation in ("denoising", "low_light_enhancement", "motion_deblurring", "detail_restoration", "colourization"):
            with self.subTest(operation):
                status, _, _ = gate(operation, NOISE, NOISE)
                self.assertTrue(status["passed"], status)
                self.assertFalse(gate(operation, NOISE + 6.0, NOISE)[0]["passed"])

    def test_face_restoration_is_never_relaxed(self):
        status, reasons, analyzer = gate("face_restoration", NOISE, NOISE, identity=VERIFIED)
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertEqual(analyzer.calls, [])

    def test_polish_keeps_its_own_gate(self):
        status, _, analyzer = gate("polish", NOISE, NOISE)
        self.assertFalse(status["passed"])
        self.assertEqual(analyzer.calls, [])

    def test_a_candidate_of_the_size_of_the_original_has_nothing_to_compare_with(self):
        """No enlargement, so the plain enlargement would be the original itself and say nothing new: the limits are what they were (0.04 for a denoise)."""
        status, reasons, analyzer = gate("denoising", 5.0, 5.0, scale=1)
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertNotIn("landmark_shift_beyond_plain_enlargement", status)
        self.assertEqual(analyzer.calls, [])

    def test_with_the_setting_at_zero_it_is_as_before(self):
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0):
            status, _, analyzer = gate("super_resolution", NOISE, NOISE)
        self.assertFalse(status["passed"])
        self.assertEqual(analyzer.calls, [])

    def test_without_the_original_pixels_or_an_analyzer_it_is_as_before(self):
        self.assertFalse(gate("super_resolution", NOISE, NOISE, with_original=False)[0]["passed"])
        self.assertFalse(gate("super_resolution", NOISE, NOISE, with_analyzer=False)[0]["passed"])

    def test_a_face_not_found_on_the_plain_enlargement_gives_no_second_opinion(self):
        status, reasons, _ = gate("super_resolution", NOISE, NOISE, plain_faces=[])
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertNotIn("landmark_shift_beyond_plain_enlargement", status)

    def test_a_face_that_is_lost_in_the_candidate_is_still_a_loss(self):
        status, reasons, analyzer = gate("super_resolution", NOISE, NOISE, candidate_faces=[])
        self.assertFalse(status["passed"])
        self.assertIn("face_lost", reasons)
        self.assertEqual(analyzer.calls, [])

    def test_the_identity_check_still_decides_identity(self):
        """The geometry gate is one of two: a candidate that passes it through the plain enlargement is still refused when the faces are no longer the same person."""
        different = SimpleNamespace(check=lambda _image, restoring=False: {"status": "failed", "similarity": 0.3, "threshold": 0.8})
        status, reasons, _ = gate("super_resolution", NOISE, NOISE, identity=different)
        self.assertFalse(status["passed"])
        self.assertIn("identity_changed", reasons)


class BiasedDetector:
    """A face detector whose landmarks on any image wider than the original are ``bias`` of the face width away: the scale-dependent placement of a small face, with no model involved."""

    def __init__(self, box, bias, original_width):
        self.box, self.bias, self.original_width = box, bias, original_width

    def detect(self, image):
        scale = image.shape[1] / self.original_width
        x, y, w, h = (int(round(value * scale)) for value in self.box)
        points = [(px * scale + (self.bias * w if scale > 1.5 else 0.0), py * scale) for px, py in base.landmarks_for(self.box)]
        return FaceDetection(boxes=[(x, y, w, h)], scores=[0.99], landmarks=[points], landmark_scores=[0.99], detector="stub", detector_version="1", confidence_type="stub")


def clear_enlargement(image, engine, variant):
    """A stand-in for the enlarger: a bicubic enlargement with a little real structure added (what a super-resolution model returns, nothing invented)."""
    scale = variant["scale"]
    enlarged = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return cv2.addWeighted(enlarged, 1.5, cv2.GaussianBlur(enlarged, (0, 0), 1.5), -0.5, 0), [{"model": engine, "device": "stand-in"}]


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class EngineTests(SimpleTestCase):
    def run_engine(self, bias):
        source = cv2.resize(base.portrait(), (320, 240))                 # a genuinely small photograph: the engine proposes the enlargement
        detector = BiasedDetector((75, 50, 90, 110), bias, 320)
        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=detector), config=EngineConfig(), runners={ops.UPSCALE: clear_enlargement})
        with patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
            output, _level, parameters = service.enhance(source)
        stage = next((s for s in parameters["adaptive"]["stages"] if s["operation"] == ops.UPSCALE), None)
        return source, output, stage, parameters

    def test_an_enlargement_the_detector_places_differently_is_not_refused_for_it(self):
        source, output, stage, parameters = self.run_engine(bias=0.08)
        self.assertIsNotNone(stage, "the engine should have proposed the enlargement")
        self.assertEqual(stage["status"], "accepted", stage.get("rejection_reasons"))
        self.assertEqual(output.shape[:2], (480, 640))
        self.assertIn(ops.UPSCALE, parameters["adaptive"]["operations_applied"])

    def test_the_same_run_without_the_rule_is_refused_for_the_geometry_alone(self):
        with override_settings(ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX=0):
            source, output, stage, _ = self.run_engine(bias=0.08)
        self.assertEqual(stage["status"], "rejected")
        self.assertEqual(stage["rejection_reasons"], ["face_geometry_changed"])
        self.assertEqual(output.shape, source.shape)

    def test_a_detector_with_no_bias_is_unchanged(self):
        _source, output, stage, _ = self.run_engine(bias=0.0)
        self.assertEqual(stage["status"], "accepted")
        self.assertEqual(output.shape[:2], (480, 640))

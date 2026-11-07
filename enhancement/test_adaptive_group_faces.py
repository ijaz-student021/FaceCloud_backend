"""A group photograph: the original's five largest faces against the candidate's five largest (ADAPTIVE_MATCH_FACES_BEYOND_REPORT).

The analyzer's report keeps the five largest faces. Among faces of nearly the same size (a row of a group photograph) the five largest of a candidate are a slightly different set from the original's
without any face being lost, and the landmark match then reported "a face that was there is no longer found" (``face_lost``). Group canvases of 8 to 24 faces, harmless candidates (a 0.5 and a 1 px blur,
an unsharp mask, a JPEG pass, a 5 % brightness change, a 6 % contrast change): 11 of 24 refused with the five largest, none with every detected face (docs/evaluation/2026-11-07-group-photographs.md).
The detector is a stand-in here (CI has no weights): these tests are about the matching rule, not about the detector.
"""
from types import SimpleNamespace

import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.validator import FinalImageQualityValidator, Limits
from .test_adaptive_landmark_floor import VERIFIED, face

WIDTH = 1400
SIZE = 100                                   # a face is about 100 px wide, 150 px apart


def box(index, size=SIZE):
    return (20 + 150 * index, 50, size, size)


class FakeDetector:
    def __init__(self, boxes):
        self.inner = base.StubDetector([(b, base.landmarks_for(b)) for b in boxes])

    def detect(self, image):
        return self.inner.detect(image)


class BrokenDetector:
    def detect(self, image):
        raise RuntimeError("no weights")


def analyze(boxes, detector=None):
    image = np.full((300, WIDTH, 3), 120, np.uint8)
    return ImageQualityAnalyzer(detector=detector or FakeDetector(boxes)).analyze(image)


class AnalyzerKeepsTheRestTests(SimpleTestCase):
    def test_the_report_keeps_the_five_largest_and_remembers_the_others(self):
        report = analyze([box(i, SIZE - 4 * i) for i in range(8)])
        self.assertEqual(len(report.faces), 5)
        self.assertEqual(len(report.other_faces), 3)
        areas = [f.box[2] * f.box[3] for f in report.faces + report.other_faces]
        self.assertEqual(areas, sorted(areas, reverse=True))
        self.assertEqual(report.faces[0].box, box(0, SIZE))

    def test_what_the_report_says_and_stores_is_unchanged(self):
        report = analyze([box(i, SIZE - 4 * i) for i in range(8)])
        self.assertEqual(report.face_count, 5)
        stored = report.as_dict()
        self.assertEqual(stored["face_count"], 5)
        self.assertEqual(len(stored["faces"]), 5)
        self.assertNotIn("other_faces", stored)

    def test_five_faces_or_fewer_leave_nothing_beyond(self):
        for count in (0, 1, 5):
            report = analyze([box(i) for i in range(count)])
            self.assertEqual(len(report.faces), count)
            self.assertEqual(report.other_faces, [])

    def test_a_detector_that_fails_gives_no_faces_beyond_either(self):
        report = analyze([], detector=BrokenDetector())
        self.assertEqual((report.faces, report.other_faces), ([], []))
        self.assertTrue(report.limitations)


def gate(candidate_faces, beyond=None, original_faces=None, operation="denoising", identity=None, with_field=True, scale=1):
    """The identity gate for a candidate ``scale`` times the original's size. The original's report keeps faces 0-4; ``candidate_faces`` are the candidate's report, ``beyond`` the faces its detector found past them."""
    original = SimpleNamespace(faces=[face(box(i)) for i in range(5)] if original_faces is None else original_faces, width=WIDTH)
    candidate = SimpleNamespace(faces=candidate_faces, width=WIDTH * scale)
    if with_field:
        candidate.other_faces = [] if beyond is None else beyond
    verdict = {"reasons": [], "gates": {}}
    FinalImageQualityValidator(None, original, identity=identity)._identity_gate(verdict, None, candidate, operation)
    return verdict["gates"]["identity"], verdict["reasons"]


def swapped_row(dx=0.0):
    """The candidate's five largest are faces 0-3 and the sixth face of the row (which grew past face 4); face 4 is found beyond them, ``dx`` px from where it was."""
    five = [face(box(i)) for i in range(4)] + [face(box(5))]
    return five, [face(box(4), dx=dx), face(box(6))]


class FaceBeyondTheReportTests(SimpleTestCase):
    def test_a_face_that_is_only_beyond_the_candidates_five_largest_is_found(self):
        five, beyond = swapped_row(dx=1.0)
        status, reasons = gate(five, beyond)
        self.assertTrue(status["passed"], status)
        self.assertNotIn("face_lost", reasons)
        self.assertAlmostEqual(status["landmark_shift"], 1.0 / SIZE / 5, places=4)     # face 4 moved 1 px of 100 and the other four 0 px: the mean over the five faces

    @override_settings(ADAPTIVE_MATCH_FACES_BEYOND_REPORT=False)
    def test_with_the_setting_off_it_is_refused_as_before(self):
        five, beyond = swapped_row()
        status, reasons = gate(five, beyond)
        self.assertFalse(status["passed"])
        self.assertIn("face_lost", reasons)
        self.assertIsNone(status["landmark_shift"])

    def test_the_setting_is_on_by_default_and_is_read_when_the_limits_are_made(self):
        self.assertTrue(Limits().match_faces_beyond_report)
        with override_settings(ADAPTIVE_MATCH_FACES_BEYOND_REPORT=False):
            self.assertFalse(Limits().match_faces_beyond_report)
        with override_settings():
            del settings.ADAPTIVE_MATCH_FACES_BEYOND_REPORT         # a settings module from before this switch existed
            self.assertTrue(Limits().match_faces_beyond_report)

    def test_a_face_that_is_gone_from_everywhere_is_still_lost(self):
        five, _beyond = swapped_row()
        far_away = [face((900, 200, SIZE, SIZE)), face((1100, 200, SIZE, SIZE))]       # faces the detector found, none within a face width of face 4
        status, reasons = gate(five, far_away)
        self.assertFalse(status["passed"])
        self.assertIn("face_lost", reasons)
        self.assertIsNone(status["landmark_shift"])

    def test_nothing_beyond_the_report_is_the_old_behaviour(self):
        five, _beyond = swapped_row()
        status, reasons = gate(five, [])
        self.assertIn("face_lost", reasons)
        self.assertFalse(status["passed"])

    def test_the_limit_is_not_loosened_for_a_face_found_beyond(self):
        five, beyond = swapped_row(dx=30.0)             # 30 px of 100 on face 4: a mean of 0.06 over the five faces, over the 0.04 limit
        status, reasons = gate(five, beyond)
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertNotIn("face_lost", reasons)
        self.assertGreater(status["landmark_shift"], status["landmark_limit"])

    def test_the_faces_beyond_are_only_asked_when_the_strict_match_fails(self):
        # Every face is found among the five largest, face 4 5 px off (a mean of 0.01 over the five); a perfect copy of face 4 sits beyond, nearer. The measured shift stays what the strict match found.
        x, y, w, h = box(4)
        five = [face(box(i)) for i in range(4)] + [face((x + 5, y, w, h))]
        status, _reasons = gate(five, [face(box(4))])
        self.assertAlmostEqual(status["landmark_shift"], 5.0 / SIZE / 5, places=4)

    def test_a_report_without_the_field_behaves_as_before(self):
        five, _beyond = swapped_row()
        status, reasons = gate(five, with_field=False)
        self.assertIn("face_lost", reasons)
        self.assertFalse(status["passed"])

    def test_every_operation_matches_the_same_way(self):
        for operation in ("denoising", "motion_deblurring", "low_light_enhancement", "super_resolution", "polish", "face_restoration"):
            five, beyond = swapped_row(dx=1.0)
            status, reasons = gate(five, beyond, operation=operation, identity=VERIFIED)
            self.assertNotIn("face_lost", reasons, operation)
            self.assertIsNotNone(status["landmark_shift"], operation)
            self.assertTrue(status["passed"], (operation, status))

    def test_an_enlarged_candidate_is_matched_on_its_own_scale(self):
        five = [face(box(i), 2) for i in range(4)] + [face(box(5), 2)]
        beyond = [face(box(4), 2, dx=2.0), face(box(6), 2)]             # 2 px of 200: 1 px of the original's 100
        status, reasons = gate(five, beyond, operation="super_resolution", scale=2)
        self.assertTrue(status["passed"], status)
        self.assertNotIn("face_lost", reasons)
        self.assertAlmostEqual(status["landmark_shift"], 1.0 / SIZE / 5, places=4)

    def test_a_restoration_still_needs_the_identity_check(self):
        five, beyond = swapped_row(dx=1.0)
        status, reasons = gate(five, beyond, operation="face_restoration", identity=None)
        self.assertFalse(status["passed"])
        self.assertIn("identity_unverifiable", reasons)

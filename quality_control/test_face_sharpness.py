"""Blur never rejects (or holds back) a photograph that has a face; sharpness is judged on the face, not the frame."""
from types import SimpleNamespace

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from enhancement.face_sharpness import primary_face_detail

from .services import acceptance_score, calculate_scores, make_decision, primary_face, with_face_sharpness

GOOD = {
    "width": 1200, "height": 900, "mean_luminance": 135.0, "contrast_stddev": 55.0, "laplacian_sharpness": 180.0,
    "estimated_noise": 2.0, "shadow_clipping_percent": 1.0, "highlight_clipping_percent": 1.0, "defects": [],
    "needs_ai_restoration": False,
}
FACE_BOX = {"x": 400, "y": 200, "width": 300, "height": 380, "area_ratio": 0.21, "confidence": 0.95}


def validation(decision="accepted", faces=(FACE_BOX,), reason=""):
    return SimpleNamespace(decision=decision, face_count=len(faces), faces=list(faces), rejection_reason=reason)


def textured(shape, seed=1):
    return np.random.default_rng(seed).integers(40, 215, size=shape, dtype=np.uint8)


def portrait(face_blur=0.0, background_blur=0.0):
    """A 1200x900 frame of generated texture: a 'face' region at FACE_BOX and a background, each optionally blurred."""
    image = textured((900, 1200, 3), 1)
    if background_blur:
        image = cv2.GaussianBlur(image, (0, 0), background_blur)
    x, y, w, h = FACE_BOX["x"], FACE_BOX["y"], FACE_BOX["width"], FACE_BOX["height"]
    face = textured((h, w, 3), 2)
    if face_blur:
        face = cv2.GaussianBlur(face, (0, 0), face_blur)
    image[y:y + h, x:x + w] = face
    return image


def frame_sharpness(image):
    return float(cv2.Laplacian(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def judge(image, face_validation):
    metrics = {**GOOD, "laplacian_sharpness": frame_sharpness(image)}
    metrics = with_face_sharpness(metrics, image, face_validation)
    scores = calculate_scores(metrics)
    return metrics, scores, make_decision(metrics, scores, face_validation)


class FaceSharpnessTests(SimpleTestCase):
    def test_the_face_measure_falls_with_blur_and_ignores_the_background(self):
        sharp = primary_face_detail(portrait(), tuple(FACE_BOX[k] for k in ("x", "y", "width", "height")))
        soft = primary_face_detail(portrait(face_blur=3), tuple(FACE_BOX[k] for k in ("x", "y", "width", "height")))
        background_only = primary_face_detail(portrait(background_blur=6), tuple(FACE_BOX[k] for k in ("x", "y", "width", "height")))
        self.assertGreater(sharp, 10 * soft)
        self.assertAlmostEqual(background_only, sharp, delta=sharp * 0.02)

    def test_a_blurred_background_with_a_sharp_face_is_accepted_with_no_blur_warning(self):
        image = portrait(background_blur=8)
        metrics, scores, (decision, reasons, _message) = judge(image, validation())
        self.assertEqual(metrics["sharpness_basis"], "face")
        self.assertFalse(metrics["face_blur_warning"])
        self.assertEqual((decision, reasons), ("accepted", []))
        self.assertGreater(scores["sharpness_score"], 90)

    def test_a_blurred_face_is_accepted_with_a_warning_never_rejected_or_reviewed(self):
        image = portrait(face_blur=4)
        metrics, scores, (decision, reasons, _message) = judge(image, validation())
        self.assertEqual(metrics["sharpness_basis"], "face")
        self.assertTrue(metrics["face_blur_warning"])
        self.assertLess(scores["sharpness_score"], 20)
        self.assertEqual((decision, reasons), ("accepted", []))
        self.assertGreaterEqual(acceptance_score(metrics, scores), 60)

    def test_the_stored_overall_score_still_shows_the_blur(self):
        _, blurred_scores, _ = judge(portrait(face_blur=4), validation())
        _, sharp_scores, _ = judge(portrait(), validation())
        self.assertLess(blurred_scores["overall_score"], sharp_scores["overall_score"])

    def test_without_a_face_the_frame_is_judged_and_severe_blur_still_rejects(self):
        image = cv2.GaussianBlur(portrait(), (0, 0), 12)   # the whole frame is blurred
        metrics, scores, (decision, reasons, _message) = judge(image, None)
        self.assertEqual(metrics["sharpness_basis"], "frame")
        self.assertEqual(decision, "rejected")
        self.assertIn("severe_blur", reasons)

    def test_a_low_confidence_face_does_not_change_the_basis(self):
        weak = {**FACE_BOX, "confidence": 0.2}
        self.assertIsNone(primary_face(validation(faces=(weak,))))
        metrics, _, _ = judge(portrait(), validation(faces=(weak,)))
        self.assertEqual(metrics["sharpness_basis"], "frame")

    def test_the_largest_confident_face_is_the_one_measured(self):
        small = {**FACE_BOX, "x": 20, "y": 20, "width": 60, "height": 70, "area_ratio": 0.004}
        self.assertEqual(primary_face(validation(faces=(small, FACE_BOX))), (400, 200, 300, 380))

    def test_a_low_resolution_photo_with_a_face_is_accepted_but_one_too_small_to_process_is_rejected(self):
        for edge, expected in ((300, "accepted"), (240, "accepted"), (239, "rejected")):
            metrics = {**GOOD, "width": edge, "height": edge, "laplacian_sharpness": 30.0, "sharpness_basis": "face"}
            decision, reasons, _ = make_decision(metrics, calculate_scores(metrics), validation())
            self.assertEqual(decision, expected, edge)
            if expected == "rejected":
                self.assertEqual(reasons, ["resolution_too_low"])

    def test_a_rejection_from_the_face_check_itself_still_rejects(self):
        metrics = {**GOOD, "sharpness_basis": "face"}
        decision, reasons, _ = make_decision(metrics, calculate_scores(metrics), validation("rejected", reason="no_face"))
        self.assertEqual((decision, reasons), ("rejected", ["no_face"]))

    def test_lighting_rules_are_untouched_in_this_phase(self):
        metrics = {**GOOD, "mean_luminance": 20.0, "sharpness_basis": "face"}
        decision, reasons, _ = make_decision(metrics, calculate_scores(metrics), validation())
        self.assertEqual(decision, "rejected")
        self.assertIn("severe_underexposure", reasons)

    @override_settings(QUALITY_FACE_SHARPNESS_TARGET=200.0)
    def test_the_face_sharpness_target_is_a_setting(self):
        metrics = {**GOOD, "laplacian_sharpness": 100.0, "sharpness_basis": "face"}
        self.assertEqual(calculate_scores(metrics)["sharpness_score"], 50.0)

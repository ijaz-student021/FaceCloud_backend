"""The denoise decision: who is eligible, and which candidates are kept.

Real CPU processing: these tests run the actual OpenCV denoiser and the actual
measurements on synthetic images. No learned model is involved. The grain is
synthetic Gaussian noise; nothing here shows behaviour on real sensor noise.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from .denoise_guard import DenoiseReference, judge_candidate
from .services import _edge_aware_denoise, enhance_bgr
from .test_pipeline_regressions import NO_MODELS, textured


def scene(height=720, width=960, base=120, seed=3):
    """Flat areas, hard edges and a patch of real (correlated) texture."""
    rng = np.random.default_rng(seed)
    image = textured(height, width, base=base).astype(np.float32)
    for _ in range(40):
        x, y = int(rng.integers(0, width - 160)), int(rng.integers(0, height - 160))
        w, h = int(rng.integers(40, 160)), int(rng.integers(40, 160))
        image[y:y + h, x:x + w] += rng.uniform(-50, 50, 3)
    weave = cv2.GaussianBlur(rng.normal(0, 1, (height, width // 3)).astype(np.float32), (0, 0), 1.4)
    image[:, : width // 3] += (weave / weave.std() * 18)[:, :, None]
    return np.clip(image, 0, 255).astype(np.uint8)


def grain(image, sigma, seed=9):
    rng = np.random.default_rng(seed)
    return np.clip(image.astype(np.float32) + rng.normal(0, sigma, image.shape), 0, 255).astype(np.uint8)


def similarity(first, second):
    from .services import _structural_similarity

    return _structural_similarity(first, second)


class MeasurementTests(SimpleTestCase):
    def test_grain_is_measured_in_the_flat_regions(self):
        clean = scene()
        self.assertLess(DenoiseReference(clean).noise_level, 1.0)
        levels = [DenoiseReference(grain(clean, sigma)).noise_level for sigma in (4, 8, 16)]
        self.assertEqual(levels, sorted(levels))
        self.assertGreater(levels[0], 1.0)

    def test_grain_hidden_by_clipping_does_not_hide_the_grain_elsewhere(self):
        """Near-black areas hold no grain; they must not be read as "flat and clean"."""
        clean = scene()
        clean[:360] = 2  # the top half is black
        noisy = grain(clean, 12)
        self.assertGreater(DenoiseReference(noisy).noise_level, 4.0)

    def test_removing_grain_leaves_the_structured_detail_measure_at_one(self):
        noisy = grain(scene(), 12)
        frame, _faces = DenoiseReference(noisy).measure(_edge_aware_denoise(noisy, 10))
        self.assertGreater(frame["noise_removed"], 1.0)
        self.assertAlmostEqual(frame["detail_retention"], 1.0, delta=0.08)
        self.assertAlmostEqual(frame["edge_retention"], 1.0, delta=0.08)
        self.assertGreater(frame["removed_residual_whiteness"], 3.0)  # what left was noise-like

    def test_the_detail_measure_does_not_depend_on_how_much_grain_there_is(self):
        """It is taken above the noise floor, so more grain does not read as more detail."""
        clean = scene()
        blurred = cv2.GaussianBlur(clean, (0, 0), 2.0)
        expected = DenoiseReference(clean).measure(blurred)[0]["detail_retention"]
        for sigma in (6, 12):
            noisy = grain(clean, sigma)
            measured = DenoiseReference(noisy).measure(cv2.GaussianBlur(noisy, (0, 0), 2.0))[0]["detail_retention"]
            self.assertAlmostEqual(measured, expected, delta=0.12, msg=sigma)

    def test_an_image_with_no_structure_reports_none_instead_of_a_made_up_ratio(self):
        flat = grain(np.full((480, 640, 3), 120, dtype=np.uint8), 10)
        frame, _faces = DenoiseReference(flat).measure(_edge_aware_denoise(flat, 10))
        self.assertIsNone(frame["detail_retention"])
        self.assertIsNone(frame["edge_retention"])

    def test_a_large_photo_is_sampled_not_scanned(self):
        large = np.zeros((3600, 4800, 3), dtype=np.uint8)
        reference = DenoiseReference(large)
        self.assertEqual(len(reference._windows), 9)
        self.assertTrue(all(w[0].stop - w[0].start == 1024 for w in reference._windows))

    def test_faces_are_measured_on_their_own(self):
        noisy = grain(scene(), 12)
        reference = DenoiseReference(noisy)
        self.assertEqual(reference.set_faces([(0.05, 0.1, 0.25, 0.4), (0.0, 0.0, 0.01, 0.01)]), 1)  # the tiny box is ignored
        _frame, faces = reference.measure(_edge_aware_denoise(noisy, 10))
        self.assertEqual(len(faces), 1)
        self.assertEqual(set(faces[0]), {"detail_retention", "edge_retention"})


class CandidateDecisionTests(SimpleTestCase):
    def judge(self, reference_image, candidate, faces=()):
        reference = DenoiseReference(reference_image)
        reference.set_faces(faces)
        return judge_candidate(reference, candidate)

    def test_real_noise_removal_is_accepted_at_full_strength(self):
        noisy = grain(scene(), 12)
        candidate = _edge_aware_denoise(noisy, 10)
        image, verdict = self.judge(noisy, candidate)
        self.assertIs(image, candidate)
        self.assertEqual((verdict["status"], verdict["applied_blend"]), ("accepted", 1.0))

    def test_a_clean_photo_is_left_alone_because_there_is_too_little_to_remove(self):
        clean = scene()
        image, verdict = self.judge(clean, _edge_aware_denoise(clean, 3))
        self.assertIs(image, clean)
        self.assertEqual(verdict["status"], "rejected")
        self.assertIn("too_little_noise_removed", verdict["rejection_reasons"])

    def test_a_candidate_that_smooths_detail_away_is_rejected_however_much_noise_it_removes(self):
        noisy = grain(scene(), 6)
        waxy = cv2.GaussianBlur(noisy, (0, 0), 2.5)
        image, verdict = self.judge(noisy, waxy)

        first = verdict["attempts"][0]
        self.assertGreater(first["frame"]["noise_removed"], 1.0)  # it did remove the noise...
        self.assertIn("detail_removed", first["violations"])      # ...and the detail with it
        self.assertIn("edges_softened", first["violations"])
        self.assertNotEqual(verdict["status"], "accepted")
        self.assertIsNot(image, waxy)

    def test_a_mildly_destructive_candidate_is_kept_at_reduced_strength(self):
        noisy = grain(scene(), 14)
        softened = cv2.GaussianBlur(noisy, (0, 0), 1.0)
        image, verdict = self.judge(noisy, softened)

        self.assertEqual(verdict["status"], "strength_reduced")
        self.assertLess(verdict["applied_blend"], 1.0)
        accepted = verdict["attempts"][-1]
        self.assertEqual(accepted["violations"], [])
        self.assertGreaterEqual(accepted["frame"]["noise_removed"], 1.0)   # still worth doing
        self.assertGreaterEqual(accepted["frame"]["detail_retention"], 0.85)
        self.assertFalse(np.array_equal(image, softened))
        self.assertFalse(np.array_equal(image, noisy))

    def test_a_weaker_blend_is_never_accepted_once_it_removes_too_little_noise(self):
        """Reducing strength cannot rescue a candidate that mostly removes detail."""
        noisy = grain(scene(), 3)
        waxy = cv2.GaussianBlur(noisy, (0, 0), 2.5)
        image, verdict = self.judge(noisy, waxy)
        self.assertEqual(verdict["status"], "rejected")
        self.assertIs(image, noisy)
        self.assertTrue(all(attempt["violations"] for attempt in verdict["attempts"]))

    def test_fine_texture_that_only_looks_like_noise_is_not_removed(self):
        rng = np.random.default_rng(4)
        field = cv2.GaussianBlur(rng.normal(0, 1, (720, 960)).astype(np.float32), (0, 0), 1.0)
        texture = cv2.cvtColor(np.clip(135 + field / field.std() * 30, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        image, verdict = self.judge(texture, _edge_aware_denoise(texture, 10))
        self.assertIs(image, texture)
        self.assertIn("removed_texture_not_noise", verdict["rejection_reasons"])

    def test_a_face_that_loses_detail_fails_the_candidate_even_if_the_frame_passes(self):
        # A small "face": flat skin with a few distinct features on it.
        clean = scene()
        left, top, right, bottom = 600, 300, 760, 500
        clean[top:bottom, left:right] = (120, 150, 190)
        for x, y, w, h in ((630, 350, 30, 12), (700, 350, 30, 12), (670, 400, 14, 40), (650, 460, 60, 10)):
            clean[y:y + h, x:x + w] = (40, 50, 70)
        noisy = grain(clean, 12)
        candidate = _edge_aware_denoise(noisy, 10).copy()
        candidate[top:bottom, left:right] = cv2.GaussianBlur(candidate[top:bottom, left:right], (0, 0), 5.0)
        face = (left / 960, top / 720, (right - left) / 960, (bottom - top) / 720)

        _image, without_face = self.judge(noisy, candidate)
        _image, with_face = self.judge(noisy, candidate, faces=[face])

        self.assertEqual(without_face["attempts"][0]["violations"], [])  # the frame as a whole is fine
        self.assertLess(with_face["attempts"][0]["faces"][0]["edge_retention"], 0.85)
        self.assertTrue(
            {"facial_detail_removed", "facial_edges_softened"} & set(with_face["attempts"][0]["violations"])
        )

    def test_every_verdict_explains_itself(self):
        noisy = grain(scene(), 12)
        for candidate in (_edge_aware_denoise(noisy, 10), cv2.GaussianBlur(noisy, (0, 0), 3.0)):
            _image, verdict = self.judge(noisy, candidate)
            self.assertTrue(verdict["reason"])
            self.assertIn("limits", verdict)
            self.assertTrue(verdict["attempts"])


@override_settings(**NO_MODELS)
class DenoiseStageTests(SimpleTestCase):
    def denoise(self, image, level="auto", **kwargs):
        output, applied, parameters = enhance_bgr(image, level, **kwargs)
        return output, parameters

    def test_brightening_alone_does_not_make_a_photo_eligible(self):
        """The regression: medium and high exposure used to imply denoising."""
        dark_but_clean = scene(base=55)
        for level in ("medium", "high", "auto"):
            with patch("enhancement.services._edge_aware_denoise") as denoiser:
                _output, parameters = self.denoise(dark_but_clean, level)
            record = parameters["adaptive_denoise"]
            self.assertFalse(record["applied"], level)
            self.assertFalse(record["eligibility"]["eligible"], level)
            self.assertIn(record["reason"], {"no_measurable_noise", "noise_score_below_ladder"}, level)
            denoiser.assert_not_called()  # not even computed

    def test_the_same_grain_is_treated_the_same_at_every_level(self):
        grainy = grain(scene(base=125), 12)
        decisions = {
            level: self.denoise(grainy, level)[1]["adaptive_denoise"]["applied"]
            for level in ("low", "medium", "high", "auto")
        }
        self.assertEqual(set(decisions.values()), {True}, decisions)

    def test_the_decision_is_reported_in_full(self):
        _output, parameters = self.denoise(grain(scene(), 12))
        record = parameters["adaptive_denoise"]

        self.assertEqual(record["reference"], "image_immediately_before_denoise")
        self.assertEqual(
            set(record["eligibility"]) >= {"eligible", "reason", "noise_score", "flat_region_noise", "ladder_strength"}, True,
        )
        self.assertEqual(
            set(record["benefit"]),
            {"noise_before", "noise_after", "noise_removed", "noise_reduction", "removed_residual_whiteness"},
        )
        self.assertEqual(set(record["detail"]), {"detail_retention", "edge_retention", "faces"})
        self.assertEqual(record["acceptance"]["status"], "accepted")
        self.assertTrue(record["acceptance"]["reason"])
        self.assertGreater(record["stage_duration_ms"], 0)
        stage = [entry for entry in parameters["stage_reports"] if entry["stage"] == "adaptive_denoise"][0]
        self.assertEqual(stage["status"], "accepted")
        self.assertEqual(stage["acceptance"], record["acceptance"])
        self.assertEqual(stage["benefit"], record["benefit"])
        self.assertGreater(stage["duration_ms"], 0)

    def test_a_rejected_candidate_leaves_the_pre_denoise_image_and_says_why(self):
        grainy = grain(scene(), 12)
        with patch("enhancement.services._edge_aware_denoise", side_effect=lambda image, strength: cv2.GaussianBlur(image, (0, 0), 4.0)):
            output, parameters = self.denoise(grainy, "low")
        with override_settings(ADAPTIVE_DENOISE_ENABLED=False):
            without_denoise, _parameters = self.denoise(grainy, "low")

        record = parameters["adaptive_denoise"]
        self.assertFalse(record["applied"])
        self.assertEqual(record["strength"], 0)
        self.assertEqual(record["reason"], "candidate_rejected")
        self.assertEqual(record["acceptance"]["status"], "rejected")
        self.assertIn("detail", record["fallback_reason"])
        np.testing.assert_array_equal(output, without_denoise)
        stage = [entry for entry in parameters["stage_reports"] if entry["stage"] == "adaptive_denoise"][0]
        self.assertEqual((stage["status"], stage["changed_pixels"]), ("rejected", False))

    def test_sharpening_is_neither_consulted_nor_changed_by_the_denoise_decision(self):
        """A loss in denoising must be judged there, not offset afterwards."""
        soft_and_grainy = grain(cv2.GaussianBlur(scene(), (0, 0), 1.6), 12)
        accepted = self.denoise(soft_and_grainy)[1]
        with patch("enhancement.services._edge_aware_denoise", side_effect=lambda image, strength: cv2.GaussianBlur(image, (0, 0), 4.0)):
            rejected = self.denoise(soft_and_grainy)[1]

        self.assertTrue(accepted["adaptive_denoise"]["applied"])
        self.assertFalse(rejected["adaptive_denoise"]["applied"])
        self.assertEqual(accepted["sharpen"], rejected["sharpen"])  # same sharpening policy either way
        # The verdict was reached on pixels sharpening had not touched yet.
        order = accepted["stage_order"]
        self.assertLess(order.index("adaptive_denoise"), order.index("sharpen"))

    def test_the_restoration_checks_still_start_from_the_accepted_denoise_output(self):
        grainy = grain(scene(), 12)
        _output, parameters = self.denoise(grainy)
        self.assertEqual(parameters["quality_guards"]["guard_b"]["reference"]["basis"], "pre_restoration_output")

    def test_denoising_real_grain_moves_the_result_toward_the_clean_photo(self):
        clean = scene(base=125)
        for sigma in (8, 16):
            grainy = grain(clean, sigma)
            output, parameters = self.denoise(grainy, "low")
            self.assertTrue(parameters["adaptive_denoise"]["applied"], sigma)
            self.assertGreater(similarity(output, clean), similarity(grainy, clean) + 0.02, sigma)

"""What the face check leaves behind, and what the results then say about it.

Classical processing; the face detector, GFPGAN and the identity guard are
replaced by stand-ins so the tests choose where the faces are and what the
models did.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import face_fidelity
from .face_fidelity import check_face_regions
from .test_staged_pipeline import WHOLE_IMAGE, skin

BOX = (60 / 320, 40 / 240, 160 / 320, 160 / 240)  # a face in the middle of a 320x240 scene


def scene():
    image = np.full((240, 320, 3), 90, dtype=np.uint8)
    image[30:210, 50:270] = cv2.resize(skin(), (220, 180))
    return image


def damaged_everywhere(image):
    """A strong skin-tone shift over the whole frame, corners of the face box included."""
    shifted = image.copy()
    shifted[:, :, 1] = np.clip(shifted[:, :, 1].astype(np.int16) + 60, 0, 255).astype(np.uint8)
    return shifted


@override_settings(FACE_FIDELITY_ENABLED=True)
class WholeBoxRevertTests(SimpleTestCase):
    def check(self, reference, candidate, boxes):
        with patch.object(face_fidelity, "_detect_boxes", return_value=boxes):
            return check_face_regions(reference, candidate)

    def test_damage_the_ellipse_blends_cannot_reach_is_removed(self):
        """P3-20: at the weakest blend the box corners kept the candidate and the
        face still measured out of limits, yet the status said "reverted"."""
        reference = scene()
        candidate = damaged_everywhere(reference)
        output, metadata = self.check(reference, candidate, [BOX])

        left, top, right, bottom = face_fidelity._pixel_box(BOX, 320, 240)
        np.testing.assert_array_equal(output[top:bottom, left:right], reference[top:bottom, left:right])
        record = metadata["faces"][0]
        self.assertEqual((record["outcome"], record["violations"]), ("reverted", []))
        self.assertEqual(record["attempts"][-1]["scope"], "whole_box")
        self.assertEqual((metadata["status"], metadata["faces_reverted"]), ("reverted", 1))

    def test_every_ellipse_blend_was_tried_first(self):
        reference = scene()
        _output, metadata = self.check(reference, damaged_everywhere(reference), [BOX])
        strengths = [attempt["strength"] for attempt in metadata["faces"][0]["attempts"]]
        self.assertEqual(strengths, [1.0, 0.7, 0.4, 0.0, 0.0])  # four ellipse blends, then the whole box
        self.assertTrue(all(attempt["violations"] for attempt in metadata["faces"][0]["attempts"][:-1]))

    def test_the_restored_box_has_no_seam(self):
        reference = scene()
        candidate = damaged_everywhere(reference)
        output, _metadata = self.check(reference, candidate, [BOX])
        left, top, right, bottom = face_fidelity._pixel_box(BOX, 320, 240)

        full_step = np.abs(candidate.astype(int) - reference.astype(int))[..., 1].max()
        # Across the box's top edge the output must change gradually, not jump
        # from the reference straight to the candidate.
        column = slice(left + 10, right - 10)
        across_edge = np.abs(output[top, column].astype(int) - output[top - 1, column].astype(int))[..., 1].max()
        self.assertLess(across_edge, full_step / 2)
        # Far from the face, the candidate is untouched.
        np.testing.assert_array_equal(output[:, 300:], candidate[:, 300:])

    def test_a_reverted_face_on_an_upscaled_grid(self):
        reference = scene()
        candidate = cv2.resize(damaged_everywhere(reference), (640, 480), interpolation=cv2.INTER_CUBIC)
        output, metadata = self.check(reference, candidate, [BOX])
        self.assertEqual(output.shape, candidate.shape)
        self.assertEqual(metadata["faces"][0]["outcome"], "reverted")
        left, top, right, bottom = face_fidelity._pixel_box(BOX, 640, 480)
        ref_left, ref_top, ref_right, ref_bottom = face_fidelity._pixel_box(BOX, 320, 240)
        back = cv2.resize(output[top:bottom, left:right], (ref_right - ref_left, ref_bottom - ref_top),
                          interpolation=cv2.INTER_AREA)
        self.assertLess(np.abs(back.astype(int) - reference[ref_top:ref_bottom, ref_left:ref_right].astype(int)).mean(), 2.0)

    def test_violations_that_survive_the_revert_are_reported_not_hidden(self):
        reference = scene()
        always = {"structural_similarity": 0.1, "texture_ratio": 1.0, "mean_chroma_shift": 0.0,
                  "highlight_clipping_growth": 0.0}
        with patch.object(face_fidelity, "face_metrics", return_value=always):
            _output, metadata = self.check(reference, damaged_everywhere(reference), [BOX])
        self.assertEqual(metadata["status"], "unresolved")
        self.assertEqual(metadata["faces"][0]["violations"], ["structural_similarity"])

    def test_a_partial_blend_is_reported_as_reduced(self):
        face = skin()
        softened = cv2.GaussianBlur(face, (0, 0), 1.0)
        _output, metadata = self.check(face, softened, WHOLE_IMAGE)
        self.assertEqual(metadata["faces"][0]["outcome"], "reduced")
        self.assertEqual(metadata["status"], "strength_reduced")


# The face check is made strict enough that a gentle restoration fails every
# blend; the identity guard confirms identity, so nothing else intervenes.
STRICT_FACE_CHECK = {"FACE_FIDELITY_ENABLED": True, "FACE_FIDELITY_MIN_STRUCTURAL_SIMILARITY": 0.99999}


def verified(source, candidate, reference=None):
    return candidate, {"status": "preserved", "applied": False, "similarity": 0.95, "reference_checked": True}


class ReportingMatchesTheImageTests(SimpleTestCase):
    """P2-11 / P3-21: what the results say must be what the saved image shows."""

    def setUp(self):
        from .test_blur_measure import blurred_photo

        self.blurred = blurred_photo()

    def run_pipeline(self, guard=verified):
        from .services import enhance_bgr
        from .test_staged_pipeline import gentle_restoration

        with patch("enhancement.services.apply_identity_guard", side_effect=guard), \
                patch("enhancement.services.build_identity_reference", return_value=(object(), "")), \
                patch("enhancement.services.restore_faces", side_effect=gentle_restoration) as restore, \
                patch.object(face_fidelity, "_detect_boxes", return_value=WHOLE_IMAGE):
            output, _level, parameters = enhance_bgr(self.blurred, "low")
        return output, parameters, restore

    def test_a_restoration_the_face_check_undid_is_not_reported_as_applied(self):
        from .test_staged_pipeline import GFPGAN_ON

        with override_settings(**GFPGAN_ON, **STRICT_FACE_CHECK):
            _output, parameters, restore = self.run_pipeline()

        restore.assert_called_once()
        self.assertEqual(parameters["face_fidelity"]["status"], "reverted")
        restoration = parameters["face_restoration"]
        self.assertFalse(restoration["applied"])
        self.assertTrue(restoration["reverted_by_face_check"])
        self.assertNotIn("gfpgan", parameters["algorithm"])
        self.assertFalse(parameters["identity_verification"]["face_restoration_applied"])
        self.assertTrue(any("face check" in warning for warning in parameters["warnings"]))
        stage = [entry for entry in parameters["stage_reports"] if entry["stage"] == "face_restoration"][-1]
        self.assertEqual(stage["status"], "reverted")

    def test_a_restoration_the_face_check_kept_is_still_reported(self):
        from .test_staged_pipeline import GFPGAN_ON

        with override_settings(**GFPGAN_ON, FACE_FIDELITY_ENABLED=True):
            _output, parameters, _restore = self.run_pipeline()

        self.assertTrue(parameters["face_restoration"]["applied"])
        self.assertIn("private_gfpgan_v1_4", parameters["algorithm"])

    def test_a_withdrawn_pass_leaves_no_trace_in_the_kept_reports(self):
        """P3-21: the first pass's reports and warnings stayed, and counted toward
        no_correction_needed."""
        from .test_staged_pipeline import GFPGAN_ON

        def inconclusive(source, candidate, reference=None):
            return candidate, {"status": "inconclusive", "applied": False, "reference_checked": True}

        with override_settings(**GFPGAN_ON, FACE_FIDELITY_ENABLED=True):
            _output, parameters, _restore = self.run_pipeline(guard=inconclusive)

        kept = [entry["stage"] for entry in parameters["stage_reports"]]
        self.assertEqual(kept.count("face_restoration"), 1)
        self.assertEqual(kept.count("identity_guard"), 1)
        discarded = [entry["stage"] for entry in parameters["discarded_stage_reports"]]
        self.assertIn("face_restoration", discarded)
        restoration_report = [entry for entry in parameters["stage_reports"] if entry["stage"] == "face_restoration"][0]
        self.assertFalse(restoration_report["changed_pixels"])
        self.assertEqual(
            parameters["no_correction_needed"],
            not any(entry["changed_pixels"] for entry in parameters["stage_reports"]),
        )
        self.assertEqual(len(parameters["warnings"]), len(set(parameters["warnings"])))

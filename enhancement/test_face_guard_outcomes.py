"""What the face check leaves behind, and what the results then say about it.

Classical processing; the face detector is replaced by a stand-in so the tests
choose where the faces are.
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

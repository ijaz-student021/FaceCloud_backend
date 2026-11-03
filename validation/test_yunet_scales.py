"""YuNet is run at two sizes and merged, so a large face in a large photograph is found (it was missed at full size: a dark portrait with a 1200 px face at 4096 px had "0 faces").

The detector here is a stand-in with the real one's behaviour on that photograph: it sees a face only when the picture it is given is at most ``limit`` pixels on its long edge.
"""
import numpy as np
from django.test import SimpleTestCase, override_settings

from .services import OpenCVYuNetFaceDetector, _merge_rows


def face_row(x, y, w, h, score):
    landmarks = [x + w * 0.3, y + h * 0.38, x + w * 0.7, y + h * 0.38, x + w * 0.5, y + h * 0.58, x + w * 0.35, y + h * 0.78, x + w * 0.65, y + h * 0.78]
    return [x, y, w, h, *landmarks, score]


class StandInYuNet:
    def __init__(self, limit=1100, rows_by_edge=None):
        self.limit, self.rows_by_edge, self.calls = limit, rows_by_edge, []

    def setInputSize(self, size):
        self.size = size

    def detect(self, image):
        height, width = image.shape[:2]
        self.calls.append((width, height))
        if self.rows_by_edge is not None:
            rows = self.rows_by_edge.get(max(width, height))
            return (1, None) if not rows else (len(rows), np.array(rows, np.float32))
        if max(width, height) > self.limit:
            return 0, None                                                  # the real detector's miss on a large face in a large picture
        return 1, np.array([face_row(width * 0.33, height * 0.30, width * 0.34, height * 0.40, 0.93)], np.float32)


def detector_with(stand_in):
    detector = OpenCVYuNetFaceDetector.__new__(OpenCVYuNetFaceDetector)       # no model file is needed: only the detect() logic is under test
    detector.detector = stand_in
    return detector


class LargeFaceInLargePhotographTests(SimpleTestCase):
    def test_the_face_of_a_4096_px_portrait_is_found_and_reported_in_the_photographs_own_pixels(self):
        stand_in = StandInYuNet()
        found = detector_with(stand_in).detect(np.zeros((2731, 4096, 3), np.uint8))
        self.assertEqual(len(found.boxes), 1)
        x, y, w, h = found.boxes[0]
        self.assertEqual((abs(x - 0.33 * 4096) <= 2, abs(y - 0.30 * 2731) <= 2, abs(w - 0.34 * 4096) <= 2, abs(h - 0.40 * 2731) <= 2), (True, True, True, True))
        self.assertEqual(stand_in.calls, [(2048, 1366), (1024, 683)])                 # two runs: at 2048 px and shrunk to 1024 px
        landmark_x, landmark_y = found.landmarks[0][0]
        self.assertAlmostEqual(landmark_x, 4096 * (0.33 + 0.34 * 0.3), delta=3)       # the landmarks are scaled back too
        self.assertAlmostEqual(landmark_y, 2731 * (0.30 + 0.40 * 0.38), delta=3)
        self.assertGreater(found.scores[0], 0.9)

    @override_settings(YUNET_LARGE_FACE_EDGE=0)
    def test_with_the_second_run_off_the_face_is_missed_as_before(self):
        stand_in = StandInYuNet()
        self.assertEqual(detector_with(stand_in).detect(np.zeros((2731, 4096, 3), np.uint8)).boxes, [])
        self.assertEqual(stand_in.calls, [(2048, 1366)])

    def test_a_photograph_under_the_limit_is_run_once_at_its_own_size_when_it_is_small(self):
        stand_in = StandInYuNet()
        found = detector_with(stand_in).detect(np.zeros((480, 640, 3), np.uint8))
        self.assertEqual((len(found.boxes), stand_in.calls), (1, [(640, 480)]))
        self.assertEqual(found.boxes[0], (round(0.33 * 640), round(0.30 * 480), round(0.34 * 640), round(0.40 * 480)))

    def test_a_photograph_between_the_two_sizes_is_run_at_its_own_size_and_shrunk(self):
        stand_in = StandInYuNet(limit=5000)
        detector_with(stand_in).detect(np.zeros((1200, 1800, 3), np.uint8))
        self.assertEqual(stand_in.calls, [(1800, 1200), (1024, 683)])

    def test_the_same_face_seen_at_both_sizes_is_one_face(self):
        found = detector_with(StandInYuNet(limit=5000)).detect(np.zeros((2731, 4096, 3), np.uint8))
        self.assertEqual(len(found.boxes), 1)

    def test_a_small_face_found_only_at_the_larger_run_and_a_large_one_only_at_the_smaller_are_both_kept(self):
        small_face = face_row(3000, 2000, 90, 110, 0.85)                  # in a 4096 px picture, found when the picture is run at 2048 px
        large_face = face_row(600, 500, 1200, 1500, 0.93)                  # found only after shrinking to 1024 px
        stand_in = StandInYuNet(rows_by_edge={2048: [[v / 2 for v in small_face[:14]] + [0.85]], 1024: [[v / 4 for v in large_face[:14]] + [0.93]]})
        found = detector_with(stand_in).detect(np.zeros((2731, 4096, 3), np.uint8))
        self.assertEqual(sorted(box[2] for box in found.boxes), [90, 1200])


class MergeRowsTests(SimpleTestCase):
    def test_the_higher_score_of_overlapping_boxes_wins_and_distinct_boxes_stay(self):
        rows = [face_row(100, 100, 200, 200, 0.80), face_row(104, 98, 200, 204, 0.95), face_row(900, 100, 200, 200, 0.70)]
        kept = _merge_rows([np.array(row) for row in rows], 0.3, 5000)
        self.assertEqual([round(float(row[14]), 2) for row in kept], [0.95, 0.70])

    def test_the_number_of_faces_is_capped(self):
        rows = [np.array(face_row(i * 300, 0, 100, 100, 0.9 - i * 0.01)) for i in range(6)]
        self.assertEqual(len(_merge_rows(rows, 0.3, 3)), 3)

    def test_defaults_are_documented_numbers(self):
        from django.conf import settings

        self.assertEqual((settings.YUNET_MAX_EDGE, settings.YUNET_LARGE_FACE_EDGE), (2048, 1024))

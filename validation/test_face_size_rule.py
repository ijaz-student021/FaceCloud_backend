"""A face is too small only when it is small in pixels as well as in the frame.

Judging by area ratio alone penalises a better camera: the same portrait shot
at a higher resolution holds the same face in more pixels but a smaller share
of the frame. A real phone portrait in testing had a 495x590 face -- larger
than any other face in the set -- that came to 3.9% of a 2386x3124 frame and
was refused as "too small".
"""
from django.test import TestCase, override_settings

from .services import MIN_FACE_AREA_RATIO, _face_is_usable


def face(width, height, frame_width, frame_height):
    return {
        "width": width,
        "height": height,
        "area_ratio": (width * height) / (frame_width * frame_height),
    }


@override_settings(FACE_MIN_EDGE_PIXELS=160)
class FaceSizeRuleTests(TestCase):
    def test_a_face_filling_enough_of_the_frame_is_usable(self):
        # 288x386 in 1251x1349 is 6.6% — over the ratio, so the pixel floor
        # never needs consulting.
        self.assertTrue(_face_is_usable(face(288, 386, 1251, 1349), MIN_FACE_AREA_RATIO))

    def test_a_large_face_in_a_large_photo_is_usable_despite_a_small_ratio(self):
        # The case that prompted this: 495x590 is only 3.9% of 2386x3124, but
        # it is the biggest face in the set and plainly enhanceable.
        subject = face(495, 590, 2386, 3124)

        self.assertLess(subject["area_ratio"], MIN_FACE_AREA_RATIO)
        self.assertTrue(_face_is_usable(subject, MIN_FACE_AREA_RATIO))

    def test_a_portrait_just_under_the_ratio_is_no_longer_refused(self):
        # 258x342 in 1448x1086 is 5.6% — under the old rule this was rejected
        # outright, with a detection confidence of 0.945.
        self.assertTrue(_face_is_usable(face(258, 342, 1448, 1086), MIN_FACE_AREA_RATIO))

    def test_a_genuinely_tiny_face_is_still_refused(self):
        # 60x80 is both a sliver of the frame and too few pixels to work with.
        subject = face(60, 80, 1200, 1600)

        self.assertLess(subject["area_ratio"], MIN_FACE_AREA_RATIO)
        self.assertFalse(_face_is_usable(subject, MIN_FACE_AREA_RATIO))

    def test_the_shorter_edge_decides_not_the_longer_one(self):
        # A tall narrow box has enough height but not enough width; a face that
        # thin is not something to enhance.
        self.assertFalse(_face_is_usable(face(80, 600, 2000, 2000), MIN_FACE_AREA_RATIO))

    def test_exactly_on_the_pixel_floor_is_accepted(self):
        self.assertTrue(_face_is_usable(face(160, 160, 4000, 4000), MIN_FACE_AREA_RATIO))

    def test_one_pixel_under_the_floor_is_not(self):
        self.assertFalse(_face_is_usable(face(159, 400, 4000, 4000), MIN_FACE_AREA_RATIO))

    @override_settings(FACE_MIN_EDGE_PIXELS=600)
    def test_the_floor_is_configurable(self):
        # Raising it past the face brings the refusal back.
        self.assertFalse(_face_is_usable(face(495, 590, 2386, 3124), MIN_FACE_AREA_RATIO))

    def test_the_ratio_alone_still_admits_a_small_but_well_framed_face(self):
        # Low resolution, but the face dominates: 100x120 of 400x300 is 10%.
        subject = face(100, 120, 400, 300)

        self.assertGreater(subject["area_ratio"], MIN_FACE_AREA_RATIO)
        self.assertTrue(_face_is_usable(subject, MIN_FACE_AREA_RATIO))

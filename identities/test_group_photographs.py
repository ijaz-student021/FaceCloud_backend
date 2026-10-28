"""A group photograph: every face big enough is grouped (up to a limit), tiny ones are skipped and said so, and the photograph is accepted either way."""
from django.test import override_settings

from .models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from .test_regrouping import Base, Provider, a_face, angle, vec


def tiny_face(x):
    """A detection far too small to be a person in a family photograph: well under 1.2% of the frame and under 160 px."""
    return a_face(x, area_ratio=0.004, width=30, height=34)


class GroupPhotographTests(Base):
    def setUp(self):
        super().setUp()
        Provider.vectors = {i: angle(i * 7) for i in range(1, 30)}
        Provider.vectors.update({100: vec(0, 0, 1), 101: vec(0, 1, 0), 102: vec(1, 0, 0)})

    def test_every_face_that_is_big_enough_gets_a_membership_whatever_the_number(self):
        image = self.photo([a_face(i) for i in range(1, 9)])
        analysis = self.analyse(image)
        self.assertEqual(analysis.status, "completed")
        self.assertEqual(IdentityFace.objects.filter(image=image).count(), 8)
        self.assertEqual(IdentityMembership.objects.filter(identity_face__image=image).count(), 8)
        self.assertEqual((analysis.evidence["faces_detected"], analysis.evidence["faces_grouped"], analysis.evidence["skipped_faces"]), (8, 8, []))

    def test_the_same_photograph_lands_in_the_folder_of_each_different_person(self):
        image = self.photo([a_face(100), a_face(101), a_face(102)])
        self.analyse(image)
        folders = IdentityGroup.objects.filter(memberships__identity_face__image=image).distinct()
        self.assertEqual(folders.count(), 3)
        for folder in folders:
            self.assertEqual(folder.memberships.filter(identity_face__image=image).count(), 1)

    def test_tiny_faces_are_skipped_and_recorded_not_grouped(self):
        image = self.photo([a_face(1), tiny_face(2), a_face(3), tiny_face(4)])
        analysis = self.analyse(image)
        self.assertEqual(analysis.status, "completed")
        self.assertEqual(IdentityFace.objects.filter(image=image).count(), 2)
        skipped = analysis.evidence["skipped_faces"]
        self.assertEqual(sorted(entry["face_index"] for entry in skipped), [1, 3])
        self.assertEqual({entry["reason"] for entry in skipped}, {"face_too_small"})

    @override_settings(IDENTITY_MAX_FACES_PER_IMAGE=3)
    def test_a_crowd_is_capped_at_the_largest_faces_and_the_rest_are_recorded(self):
        faces = [a_face(i, area_ratio=0.30 - i * 0.01) for i in range(1, 9)]              # already largest first, as the face check sorts them
        image = self.photo(faces)
        analysis = self.analyse(image)
        self.assertEqual(analysis.status, "completed")
        self.assertEqual(IdentityFace.objects.filter(image=image).count(), 3)
        self.assertEqual(sorted(IdentityFace.objects.filter(image=image).values_list("face_index", flat=True)), [0, 1, 2])
        skipped = analysis.evidence["skipped_faces"]
        self.assertEqual([entry["face_index"] for entry in skipped], [3, 4, 5, 6, 7])
        self.assertEqual({(entry["reason"], entry["limit"]) for entry in skipped}, {("too_many_faces", 3)})
        self.assertEqual(analysis.evidence["faces_detected"], 8)

    @override_settings(IDENTITY_MAX_FACES_PER_IMAGE=3)
    def test_small_faces_do_not_use_up_the_cap(self):
        image = self.photo([a_face(1), tiny_face(2), tiny_face(3), a_face(4), a_face(5), a_face(6)])
        self.analyse(image)
        self.assertEqual(sorted(IdentityFace.objects.filter(image=image).values_list("face_index", flat=True)), [0, 3, 4])

    def test_the_cap_has_a_sane_default_and_is_validated(self):
        from django.conf import settings

        self.assertEqual(settings.IDENTITY_MAX_FACES_PER_IMAGE, 20)

    def test_a_single_unusable_face_is_still_not_grouped_but_the_analysis_completes(self):
        image = self.photo([tiny_face(1)])
        analysis = self.analyse(image)
        self.assertEqual(analysis.status, IdentityAnalysis.Status.COMPLETED)
        self.assertFalse(IdentityFace.objects.filter(image=image).exists())

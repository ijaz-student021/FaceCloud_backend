"""A person who gives face-processing consent after uploading has the earlier photographs grouped, without picking them by hand.

Photographs uploaded before the consent are checked and enhanced but never analysed. Granting consent now queues ``identities.group_held_photos``, which tries the owner's own
photographs that have a face, were never analysed and are not rejected.
"""
from unittest.mock import patch

from django.test import override_settings

from privacy.models import ConsentPurpose
from privacy.services import current_policy_version, record_consent

from .models import IdentityAnalysis
from .test_regrouping import Base, a_face
from .verdict import group_held_photos


class GroupHeldPhotosTests(Base):
    def tried(self):
        with patch("identities.verdict.group_when_enhancement_ends") as group:
            count = group_held_photos(self.owner)
        return count, {call.args[0].pk for call in group.call_args_list}

    def test_only_the_owners_unanalysed_photographs_with_a_face_are_tried(self):
        waiting = self.photo([a_face(100)], quality="accepted")
        no_face = self.photo([], quality="accepted")
        rejected = self.photo([a_face(100)], status="rejected")
        done = self.photo([a_face(100)], quality="accepted")
        IdentityAnalysis.objects.create(owner=self.owner, image=done, preprocessing_result=done.preprocessing_result, face_validation=done.face_validation, status="completed", provider="p", policy_version="t")
        count, tried = self.tried()
        self.assertEqual(tried, {waiting.pk})
        self.assertEqual(count, 1)
        self.assertNotIn(no_face.pk, tried)
        self.assertNotIn(rejected.pk, tried)

    def test_another_persons_photographs_are_left_alone(self):
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(username="held-other", email="held-other@example.com", password="Password!123")
        mine = self.photo([a_face(100)], quality="accepted")
        theirs = self.photo([a_face(100)], quality="accepted")
        type(theirs).objects.filter(pk=theirs.pk).update(owner=other)
        _count, tried = self.tried()
        self.assertEqual(tried, {mine.pk})

    def test_the_limit_caps_one_pass(self):
        for _ in range(3):
            self.photo([a_face(100)], quality="accepted")
        with patch("identities.verdict.group_when_enhancement_ends") as group:
            self.assertEqual(group_held_photos(self.owner, limit=2), 2)
        self.assertEqual(group.call_count, 2)


@override_settings(BIOMETRIC_CONSENT_REQUIRED=True)
class ConsentQueuesTheGroupingTests(Base):
    def setUp(self):
        super().setUp()
        self.person = type(self.owner).objects.create_user(username="held-new", email="held-new@example.com", password="Password!123")

    def grant(self, granted=True):
        with patch("identities.tasks.group_held_photos_task.delay") as queued, self.captureOnCommitCallbacks(execute=True):
            record_consent(self.person, ConsentPurpose.FACE_IDENTITY, granted, current_policy_version())
        return queued

    def test_granting_consent_queues_the_grouping_of_earlier_photographs(self):
        queued = self.grant(True)
        queued.assert_called_once_with(self.person.pk)

    def test_asking_again_with_the_same_answer_queues_nothing(self):
        self.grant(True)
        self.assertFalse(self.grant(True).called)

    def test_withdrawing_consent_queues_nothing(self):
        self.grant(True)
        self.assertFalse(self.grant(False).called)

    def test_a_queue_problem_does_not_undo_the_answer(self):
        with patch("identities.tasks.group_held_photos_task.delay", side_effect=RuntimeError("broker down")), self.captureOnCommitCallbacks(execute=True):
            row, changed = record_consent(self.person, ConsentPurpose.FACE_IDENTITY, True, current_policy_version())
        self.assertTrue(changed)
        self.assertTrue(row.granted)

    def test_the_task_groups_through_the_service(self):
        from .tasks import group_held_photos_task

        with patch("identities.verdict.group_held_photos", return_value=3) as grouped:
            result = group_held_photos_task.apply(args=[self.person.pk]).get()
        grouped.assert_called_once()
        self.assertEqual(result, {"user_id": self.person.pk, "tried": 3})
        self.assertEqual(group_held_photos_task.apply(args=[999999]).get()["tried"], 0)

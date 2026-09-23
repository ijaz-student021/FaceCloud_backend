"""review_membership() had zero test coverage before T-007's moderator
write-back (see moderation/services.py's decide_review()). Covers both the
plain self-review path (previously untested) and the reviewer/actor split
that lets a moderator decide on the content owner's behalf without changing
who group ownership, candidate-group scoping, or IdentityChange.owner get
attributed to.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup, IdentityMembership
from .services import review_membership
from .storage import protect_embedding

User = get_user_model()


class ReviewMembershipTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="review-owner", email="review-owner@example.com", password="Password!123",
        )
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="face.jpg", status="uploaded",
        )
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.image, status="completed",
        )
        validation = FaceValidationResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        self.analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            face_validation=validation, status="needs_review", provider="test",
            policy_version="test-v1", review_count=1,
        )
        self.face = IdentityFace.objects.create(
            owner=self.owner, image=self.image, analysis=self.analysis, face_index=0,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        self.group = IdentityGroup.objects.create(
            owner=self.owner, display_name="Person 001", embedding_dimension=3, provider="test",
            policy_version="test-v1", protected_centroid=protect_embedding([1.0, 0.0, 0.0]), member_count=1,
        )
        self.membership = IdentityMembership.objects.create(
            owner=self.owner, identity_face=self.face, identity_group=self.group,
            state=IdentityMembership.State.NEEDS_REVIEW, decision_method="test", similarity_score=0.75,
        )

    def make_moderator(self):
        return User.objects.create_user(
            username="review-moderator", email="review-moderator@example.com", password="Password!123",
        )

    # -- self-review (actor defaults to reviewer) ----------------------------

    def test_confirming_as_the_owner_records_the_owner_as_reviewer_and_actor(self):
        result = review_membership(self.membership, self.owner, "confirm")

        self.assertEqual(result.state, IdentityMembership.State.CONFIRMED)
        self.assertEqual(result.reviewed_by, self.owner)
        change = IdentityChange.objects.get(membership=result)
        self.assertEqual(change.owner, self.owner)
        self.assertEqual(change.actor, self.owner)

    def test_rejecting_does_not_require_a_suggested_group(self):
        self.membership.identity_group = None
        self.membership.save(update_fields=["identity_group"])

        result = review_membership(self.membership, self.owner, "reject")

        self.assertEqual(result.state, IdentityMembership.State.REJECTED)

    def test_confirming_with_no_suggested_group_is_rejected(self):
        self.membership.identity_group = None
        self.membership.save(update_fields=["identity_group"])

        with self.assertRaises(ValidationError):
            review_membership(self.membership, self.owner, "confirm")

    def test_reviewing_an_already_decided_membership_is_rejected(self):
        review_membership(self.membership, self.owner, "confirm")

        with self.assertRaises(ValidationError):
            review_membership(self.membership, self.owner, "confirm")

    # -- moderator-authorized review (actor != reviewer) ---------------------

    def test_a_moderator_deciding_on_behalf_of_the_owner_is_recorded_as_the_actor(self):
        moderator = self.make_moderator()

        result = review_membership(self.membership, self.owner, "confirm", actor=moderator)

        self.assertEqual(result.reviewed_by, moderator)
        change = IdentityChange.objects.get(membership=result)
        self.assertEqual(change.owner, self.owner, "content ownership must stay with the real owner")
        self.assertEqual(change.actor, moderator, "the audit actor must be whoever actually decided")

    def test_a_moderator_starting_a_new_group_attributes_its_ownership_to_the_content_owner(self):
        moderator = self.make_moderator()
        self.membership.identity_group = None
        self.membership.save(update_fields=["identity_group"])

        result = review_membership(self.membership, self.owner, "new_group", actor=moderator)

        self.assertEqual(result.identity_group.owner, self.owner)
        self.assertNotEqual(result.identity_group.owner, moderator)

    def test_a_moderator_selecting_a_group_is_scoped_to_the_owners_groups_not_the_moderators(self):
        moderator = self.make_moderator()
        moderators_group = IdentityGroup.objects.create(
            owner=moderator, display_name="Not Yours", embedding_dimension=3,
            provider="test", policy_version="test-v1",
        )
        self.membership.identity_group = None
        self.membership.save(update_fields=["identity_group"])

        with self.assertRaises(ValidationError):
            review_membership(
                self.membership, self.owner, "select_group",
                group_id=moderators_group.pk, actor=moderator,
            )

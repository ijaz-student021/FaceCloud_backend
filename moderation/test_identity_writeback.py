"""T-007: a moderator's decision on an identity review writes back into the
underlying IdentityMembership, the same way quality/duplicate decisions
already do -- see moderation/services.py's decide_review() and
identities/services.py's review_membership() for the reviewer/actor split
that makes this safe (group ownership stays with the content's real owner;
only reviewed_by/IdentityChange.actor record the deciding moderator).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from identities.models import IdentityChange, IdentityGroup, IdentityMembership
from identities.storage import protect_embedding
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ModeratorReview

User = get_user_model()


class IdentityWritebackFixtureMixin:
    def make_user(self, name, **extra):
        defaults = {"is_email_verified": True, "email": f"{name}@example.com"}
        defaults.update(extra)
        return User.objects.create_user(username=name, password="ForestRiver!4821", **defaults)

    def authenticated_client(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def make_identity_review(self, owner, organization, with_suggested_group=True):
        from identities.models import IdentityAnalysis, IdentityFace

        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name="face.jpg", status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        analysis = IdentityAnalysis.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, face_validation=validation,
            status="needs_review", provider="test", policy_version="test-v1", review_count=1,
        )
        face = IdentityFace.objects.create(
            owner=owner, image=image, analysis=analysis, face_index=0,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        group = None
        if with_suggested_group:
            group = IdentityGroup.objects.create(
                owner=owner, organization=organization, display_name="Person 001",
                embedding_dimension=3, provider="test", policy_version="test-v1",
                protected_centroid=protect_embedding([1.0, 0.0, 0.0]), member_count=1,
            )
        membership = IdentityMembership.objects.create(
            owner=owner, identity_face=face, identity_group=group,
            state=IdentityMembership.State.NEEDS_REVIEW, decision_method="test", similarity_score=0.75,
        )
        review = ModeratorReview.objects.create(
            organization=organization, image=image, source=ModeratorReview.Source.IDENTITY,
            ai_recommendation="ambiguous_centroid_cosine_v1", identity_membership=membership,
        )
        return review, membership


class IdentityReviewWritebackTests(IdentityWritebackFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("identity-mod-owner")
        self.organization = create_organization(name="Identity Mod Co", owner=self.owner)
        self.moderator = self.make_user("identity-mod-moderator")
        from organizations.models import OrganizationMembership
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator, role=OrganizationMembership.Role.MODERATOR,
        )

    def decision_url(self, review):
        return reverse(
            "moderation:review-decision",
            kwargs={"organization_id": self.organization.pk, "review_id": review.pk},
        )

    def test_approving_confirms_the_membership_with_its_suggested_group(self):
        review, membership = self.make_identity_review(self.owner, self.organization)

        response = self.authenticated_client(self.moderator).post(
            self.decision_url(review), {"action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertEqual(membership.state, IdentityMembership.State.CONFIRMED)

    def test_the_deciding_moderator_is_recorded_as_reviewed_by_not_the_owner(self):
        review, membership = self.make_identity_review(self.owner, self.organization)

        self.authenticated_client(self.moderator).post(self.decision_url(review), {"action": "approve"}, format="json")

        membership.refresh_from_db()
        self.assertEqual(membership.reviewed_by, self.moderator)
        change = IdentityChange.objects.get(membership=membership)
        self.assertEqual(change.owner, self.owner)
        self.assertEqual(change.actor, self.moderator)

    def test_rejecting_rejects_the_membership(self):
        review, membership = self.make_identity_review(self.owner, self.organization)

        response = self.authenticated_client(self.moderator).post(
            self.decision_url(review), {"action": "reject"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertEqual(membership.state, IdentityMembership.State.REJECTED)

    def test_keeping_in_review_does_not_touch_the_membership(self):
        review, membership = self.make_identity_review(self.owner, self.organization)

        response = self.authenticated_client(self.moderator).post(
            self.decision_url(review), {"action": "keep_in_review"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertEqual(membership.state, IdentityMembership.State.NEEDS_REVIEW)

    def test_correcting_with_a_group_id_reassigns_to_that_group(self):
        review, membership = self.make_identity_review(self.owner, self.organization)
        alternate_group = IdentityGroup.objects.create(
            owner=self.owner, organization=self.organization, display_name="Person 002",
            embedding_dimension=3, provider="test", policy_version="test-v1",
        )

        response = self.authenticated_client(self.moderator).post(
            self.decision_url(review), {"action": "correct", "group_id": alternate_group.pk}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertEqual(membership.identity_group_id, alternate_group.pk)
        self.assertEqual(membership.state, IdentityMembership.State.CONFIRMED)

    def test_correcting_without_a_group_id_starts_a_new_group_owned_by_the_content_owner(self):
        review, membership = self.make_identity_review(self.owner, self.organization, with_suggested_group=False)

        response = self.authenticated_client(self.moderator).post(
            self.decision_url(review), {"action": "correct"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertIsNotNone(membership.identity_group_id)
        self.assertEqual(membership.identity_group.owner, self.owner)

    def test_correcting_an_identity_review_tells_the_member_whose_photograph_it_is(self):
        from notifications.models import UserNotification

        member = self.make_user("identity-mod-member")
        OrganizationMembership.objects.create(organization=self.organization, user=member, role="member", status="active")
        review, membership = self.make_identity_review(member, self.organization, with_suggested_group=False)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.authenticated_client(self.moderator).post(
                self.decision_url(review), {"action": "correct", "message_to_member": "I put you in your own folder."}, format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        (note,) = UserNotification.objects.filter(recipient=member, event_type__startswith="photo_review")
        self.assertEqual((note.event_type, note.message, note.resource_id), ("photo_review_approved", "I put you in your own folder.", review.image_id))

    def test_approving_with_no_suggested_group_returns_a_client_error_not_a_silent_success(self):
        review, membership = self.make_identity_review(self.owner, self.organization, with_suggested_group=False)

        response = self.authenticated_client(self.moderator).post(
            self.decision_url(review), {"action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        membership.refresh_from_db()
        self.assertEqual(membership.state, IdentityMembership.State.NEEDS_REVIEW)

    def test_the_owner_can_also_decide_their_own_identity_review_as_organization_owner(self):
        review, membership = self.make_identity_review(self.owner, self.organization)

        response = self.authenticated_client(self.owner).post(
            self.decision_url(review), {"action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership.refresh_from_db()
        self.assertEqual(membership.reviewed_by, self.owner)

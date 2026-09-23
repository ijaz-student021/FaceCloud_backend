"""T-007 Phase G: moderator-driven merge of the identity groups behind a
flagged duplicate match. There is no DuplicateMatch-level merge concept --
the real primitive is identities.management.merge_groups(), extended here
with the same reviewer/actor split review_membership() already got (see
that function's docstring).
"""
from django.urls import reverse
from django.test import TestCase
from rest_framework import status

from duplicates.models import DuplicateAnalysis, DuplicateMatch
from identities.models import IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup, IdentityMembership
from identities.storage import protect_embedding
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ModeratorReview
from .services import groups_for_duplicate_match
from .tests import ModerationFixtureMixin


class DuplicateGroupMergeFixtureMixin(ModerationFixtureMixin):
    def make_identified_image(self, owner, organization, batch, name, group=None):
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=name, status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        analysis = IdentityAnalysis.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, face_validation=validation,
            status="completed", provider="test", policy_version="test-v1",
        )
        face = IdentityFace.objects.create(
            owner=owner, image=image, analysis=analysis, face_index=0,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        if group is None:
            group = IdentityGroup.objects.create(
                owner=owner, organization=organization, display_name=f"Person {name}",
                embedding_dimension=3, provider="test", policy_version="test-v1",
                protected_centroid=protect_embedding([1.0, 0.0, 0.0]), member_count=1,
            )
        IdentityMembership.objects.create(
            owner=owner, identity_face=face, identity_group=group,
            state=IdentityMembership.State.CONFIRMED, decision_method="test", similarity_score=0.9,
        )
        return image, group

    def make_duplicate_review(self, owner, organization, source_group=None, matched_group=None):
        batch = UploadBatch.objects.create(owner=owner, upload_type="multiple", organization=organization)
        source_image, source_group = self.make_identified_image(owner, organization, batch, "a.jpg", source_group)
        matched_image, matched_group = self.make_identified_image(owner, organization, batch, "b.jpg", matched_group)
        preprocessing = ImagePreprocessingResult.objects.get(image=source_image)
        analysis = DuplicateAnalysis.objects.create(
            owner=owner, image=source_image, preprocessing_result=preprocessing, status="completed",
        )
        match = DuplicateMatch.objects.create(
            owner=owner, analysis=analysis, source_image=source_image, matched_image=matched_image,
            match_type=DuplicateMatch.MatchType.PERCEPTUAL, similarity_score=0.85,
            review_status=DuplicateMatch.ReviewStatus.SUSPECTED,
        )
        review = ModeratorReview.objects.create(
            organization=organization, image=source_image, source=ModeratorReview.Source.DUPLICATE,
            ai_recommendation="perceptual", duplicate_match=match,
        )
        return review, match, source_group, matched_group


class GroupsForDuplicateMatchTests(DuplicateGroupMergeFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("resolve-owner")
        self.organization = create_organization(name="Resolve Co", owner=self.owner)

    def test_each_side_resolves_to_its_own_group(self):
        _, match, source_group, matched_group = self.make_duplicate_review(self.owner, self.organization)

        source_candidates, matched_candidates = groups_for_duplicate_match(match)

        self.assertEqual([group.pk for group in source_candidates], [source_group.pk])
        self.assertEqual([group.pk for group in matched_candidates], [matched_group.pk])


class ModeratorDuplicateGroupMergeApiTests(DuplicateGroupMergeFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("merge-owner")
        self.organization = create_organization(name="Merge Co", owner=self.owner)
        self.moderator_user = self.make_user("merge-moderator")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator_user, role=OrganizationMembership.Role.MODERATOR,
        )

    def merge_url(self, review):
        return reverse(
            "moderation:review-merge-identity-groups",
            kwargs={"organization_id": self.organization.pk, "review_id": review.pk},
        )

    def test_a_moderator_can_merge_the_groups_behind_an_unambiguous_duplicate(self):
        review, _, source_group, target_group = self.make_duplicate_review(self.owner, self.organization)

        response = self.authenticated_client(self.moderator_user).post(self.merge_url(review), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        source_group.refresh_from_db()
        target_group.refresh_from_db()
        self.assertEqual(source_group.state, IdentityGroup.State.MERGED)
        self.assertEqual(target_group.member_count, 2)

    def test_group_ownership_stays_with_the_real_owner_not_the_moderator(self):
        review, _, source_group, target_group = self.make_duplicate_review(self.owner, self.organization)

        self.authenticated_client(self.moderator_user).post(self.merge_url(review), {}, format="json")

        change = IdentityChange.objects.get(action=IdentityChange.Action.MERGED, group_id=target_group.pk)
        self.assertEqual(change.owner, self.owner)
        self.assertEqual(change.actor, self.moderator_user)

    def test_the_merge_is_audited_on_the_organization(self):
        review, _, _, target_group = self.make_duplicate_review(self.owner, self.organization)

        self.authenticated_client(self.moderator_user).post(
            self.merge_url(review), {"reason": "confirmed same person"}, format="json",
        )

        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=self.organization,
                action=OrganizationAuditEvent.Action.IDENTITY_GROUPS_MERGED_BY_MODERATOR,
                target_id=target_group.pk,
            ).exists()
        )

    def test_merging_a_quality_review_is_rejected(self):
        review, _ = self.make_quality_review(self.owner, self.organization)

        response = self.authenticated_client(self.moderator_user).post(self.merge_url(review), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_both_sides_already_share_one_group_the_merge_is_rejected(self):
        _, group = self.make_identified_image(
            self.owner, self.organization,
            UploadBatch.objects.create(owner=self.owner, upload_type="multiple", organization=self.organization),
            "shared.jpg",
        )
        review, _, _, _ = self.make_duplicate_review(self.owner, self.organization, source_group=group, matched_group=group)

        response = self.authenticated_client(self.moderator_user).post(self.merge_url(review), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_explicit_group_id_not_linked_to_the_match_is_rejected(self):
        review, _, source_group, _ = self.make_duplicate_review(self.owner, self.organization)
        unrelated_group = IdentityGroup.objects.create(
            owner=self.owner, organization=self.organization, display_name="Unrelated",
            embedding_dimension=3, provider="test", policy_version="test-v1",
        )

        response = self.authenticated_client(self.moderator_user).post(
            self.merge_url(review), {"target_group_id": unrelated_group.pk}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_plain_member_cannot_merge_groups(self):
        member = self.make_user("merge-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )
        review, _, _, _ = self.make_duplicate_review(self.owner, self.organization)

        response = self.authenticated_client(member).post(self.merge_url(review), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_review_from_another_organization_is_a_404(self):
        other_owner = self.make_user("merge-other-owner")
        other_org = create_organization(name="Merge Other Co", owner=other_owner)
        review, _, _, _ = self.make_duplicate_review(other_owner, other_org)

        response = self.authenticated_client(self.owner).post(self.merge_url(review), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

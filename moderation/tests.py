"""T-007 Phase C: the moderator decision/escalate/dashboard API surface.

Uses ModeratorReview rows built directly (not through the create_review_for_*
hooks, which are covered in test_hooks.py) so these tests focus purely on the
API layer: org-scoping, role gates, decision write-back, and the audit trail.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from duplicates.models import DuplicateAnalysis, DuplicateMatch
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import ModeratorReview

User = get_user_model()


class ModerationFixtureMixin:
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

    def make_image(self, owner, organization):
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        return ImageUpload.objects.create(owner=owner, batch=batch, original_name="face.jpg", status="uploaded")

    def make_quality_review(self, owner, organization, image=None):
        image = image or self.make_image(owner, organization)
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        decision = ImageQualityDecision.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, status="completed",
            decision=ImageQualityDecision.Decision.NEEDS_REVIEW,
        )
        review = ModeratorReview.objects.create(
            organization=organization, image=image, source=ModeratorReview.Source.QUALITY,
            ai_recommendation="needs_review", quality_decision=decision,
        )
        return review, decision

    def make_duplicate_review(self, owner, organization):
        image = self.make_image(owner, organization)
        candidate = ImageUpload.objects.create(
            owner=owner, batch=image.batch, original_name="b.jpg", status="uploaded",
        )
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        analysis = DuplicateAnalysis.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, status="completed",
        )
        match = DuplicateMatch.objects.create(
            owner=owner, analysis=analysis, source_image=image, matched_image=candidate,
            match_type=DuplicateMatch.MatchType.PERCEPTUAL, similarity_score=0.7,
            review_status=DuplicateMatch.ReviewStatus.SUSPECTED,
        )
        review = ModeratorReview.objects.create(
            organization=organization, image=image, source=ModeratorReview.Source.DUPLICATE,
            ai_recommendation="perceptual", duplicate_match=match,
        )
        return review, match


class ModeratorReviewIsolationTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("mod-owner")
        self.organization = create_organization(name="Mod Co", owner=self.owner)
        self.review, _ = self.make_quality_review(self.owner, self.organization)
        self.owner_client = self.authenticated_client(self.owner)

    def test_a_plain_member_cannot_list_reviews(self):
        member = self.make_user("mod-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )
        response = self.authenticated_client(member).get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_unrelated_user_gets_404(self):
        outsider = self.authenticated_client(self.make_user("mod-outsider"))
        response = outsider.get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_review_from_another_organization_is_not_visible(self):
        other_owner = self.make_user("other-org-owner")
        other_org = create_organization(name="Other Co", owner=other_owner)
        other_review, _ = self.make_quality_review(other_owner, other_org)

        response = self.owner_client.get(
            reverse(
                "moderation:review-detail",
                kwargs={"organization_id": self.organization.pk, "review_id": other_review.pk},
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_owner_can_list_and_see_the_review(self):
        response = self.owner_client.get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in response.data["results"]] if "results" in response.data else [
            row["id"] for row in response.data
        ]
        self.assertIn(self.review.pk, ids)


class QualityDecisionWritebackTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("quality-mod-owner")
        self.organization = create_organization(name="Quality Co", owner=self.owner)
        self.review, self.decision = self.make_quality_review(self.owner, self.organization)
        self.client = self.authenticated_client(self.owner)

    def decision_url(self):
        return reverse(
            "moderation:review-decision",
            kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
        )

    def test_approving_a_quality_review_accepts_the_underlying_decision(self):
        response = self.client.post(self.decision_url(), {"action": "approve"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.decision.refresh_from_db()
        self.assertEqual(self.review.status, ModeratorReview.Status.APPROVED)
        self.assertIsNotNone(self.review.decided_at)
        self.assertEqual(self.review.decided_by, self.owner)
        self.assertEqual(self.decision.decision, ImageQualityDecision.Decision.ACCEPTED)
        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=self.organization, action=OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
                target_id=self.review.pk,
            ).exists()
        )

    def test_rejecting_a_quality_review_rejects_the_underlying_decision(self):
        response = self.client.post(self.decision_url(), {"action": "reject", "reason": "still blurry"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.decision, ImageQualityDecision.Decision.REJECTED)

    def test_keeping_a_review_in_review_does_not_finalize_it(self):
        response = self.client.post(self.decision_url(), {"action": "keep_in_review"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ModeratorReview.Status.IN_REVIEW)
        self.assertIsNone(self.review.decided_at)

    def test_a_second_decision_after_a_final_one_is_a_conflict(self):
        self.client.post(self.decision_url(), {"action": "approve"}, format="json")

        response = self.client.post(self.decision_url(), {"action": "reject"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_an_unknown_action_is_rejected(self):
        response = self.client.post(self.decision_url(), {"action": "reprocess"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class DuplicateDecisionWritebackTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("dup-mod-owner")
        self.organization = create_organization(name="Dup Co", owner=self.owner)
        self.review, self.match = self.make_duplicate_review(self.owner, self.organization)
        self.client = self.authenticated_client(self.owner)

    def test_approving_a_duplicate_review_confirms_the_match(self):
        response = self.client.post(
            reverse(
                "moderation:review-decision",
                kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
            ),
            {"action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.match.refresh_from_db()
        self.assertEqual(self.match.review_status, DuplicateMatch.ReviewStatus.CONFIRMED)
        self.assertEqual(self.match.reviewed_by, self.owner)

    def test_rejecting_a_duplicate_review_dismisses_the_match(self):
        response = self.client.post(
            reverse(
                "moderation:review-decision",
                kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
            ),
            {"action": "reject"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.match.refresh_from_db()
        self.assertEqual(self.match.review_status, DuplicateMatch.ReviewStatus.NOT_DUPLICATE)


class EscalationAndDashboardTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("dash-owner")
        self.organization = create_organization(name="Dash Co", owner=self.owner)
        self.client = self.authenticated_client(self.owner)

    def test_escalating_a_review_sets_its_status_and_assigns_the_escalator(self):
        review, _ = self.make_quality_review(self.owner, self.organization)

        response = self.client.post(
            reverse(
                "moderation:review-escalate",
                kwargs={"organization_id": self.organization.pk, "review_id": review.pk},
            ),
            {"reason": "needs a second opinion"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        review.refresh_from_db()
        self.assertEqual(review.status, ModeratorReview.Status.ESCALATED)
        self.assertEqual(review.assigned_moderator, self.owner)
        self.assertIsNone(review.decided_at)

    def test_dashboard_counts_reflect_review_status_scoped_to_this_organization(self):
        pending, _ = self.make_quality_review(self.owner, self.organization)
        approved, _ = self.make_quality_review(self.owner, self.organization)
        self.client.post(
            reverse(
                "moderation:review-decision",
                kwargs={"organization_id": self.organization.pk, "review_id": approved.pk},
            ),
            {"action": "approve"}, format="json",
        )
        other_owner = self.make_user("dash-other-owner")
        other_org = create_organization(name="Other Dash Co", owner=other_owner)
        self.make_quality_review(other_owner, other_org)

        response = self.client.get(
            reverse("moderation:dashboard", kwargs={"organization_id": self.organization.pk}),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["pending"], 1)
        self.assertEqual(response.data["approved_total"], 1)

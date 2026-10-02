"""ai_confidence means one thing, whatever raised the review.

The three sources disagree among themselves — a quality score and a duplicate
similarity are already 0-100, an identity similarity is 0-1 — so a queue
rendering the field had no way to know which it was holding. These tests pin it
to a percentage at the point the review is created.
"""
from django.test import TestCase
from django.urls import reverse

from identities.models import IdentityAnalysis, IdentityFace, IdentityGroup, IdentityMembership
from identities.storage import protect_embedding
from validation.models import FaceValidationResult
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult

from .models import ModeratorReview
from .services import (
    create_review_for_duplicate_match,
    create_review_for_identity_membership,
    create_review_for_quality_decision,
)
from .tests import ModerationFixtureMixin


class ConfidenceScaleTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("scale-owner")
        self.organization = create_organization(name="Scale Co", owner=self.owner)

    def test_a_quality_score_is_kept_as_the_percentage_it_already_is(self):
        image = self.make_image(self.owner, self.organization)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=image, status="completed",
        )
        decision = ImageQualityDecision.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing,
            status="completed", decision=ImageQualityDecision.Decision.NEEDS_REVIEW,
            overall_score=85.44,
        )

        review = create_review_for_quality_decision(decision)

        self.assertAlmostEqual(review.ai_confidence, 85.44, places=2)

    def test_a_duplicate_similarity_is_kept_as_the_percentage_it_already_is(self):
        review, match = self.make_duplicate_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).delete()
        match.similarity_score = 92.5
        match.save(update_fields=["similarity_score"])

        created = create_review_for_duplicate_match(match)

        self.assertAlmostEqual(created.ai_confidence, 92.5, places=2)

    def make_identity_membership(self, similarity):
        image = self.make_image(self.owner, self.organization)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=image, status="completed",
        )
        validation = FaceValidationResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1,
            faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing,
            face_validation=validation, status="needs_review", provider="test",
            policy_version="test-v1", review_count=1,
        )
        face = IdentityFace.objects.create(
            owner=self.owner, analysis=analysis, image=image, face_index=0,
            protected_embedding=protect_embedding([1.0, 0.0, 0.0]),
            embedding_dimension=3, provider="test",
        )
        group = IdentityGroup.objects.create(
            owner=self.owner, organization=self.organization, display_name="Person A",
            embedding_dimension=3, provider="test", policy_version="test-v1",
            protected_centroid=protect_embedding([1.0, 0.0, 0.0]), member_count=1,
        )
        return IdentityMembership.objects.create(
            owner=self.owner, identity_face=face, identity_group=group,
            similarity_score=similarity, state="needs_review", decision_method="ambiguous",
        )

    def test_an_identity_similarity_is_converted_from_a_fraction(self):
        membership = self.make_identity_membership(0.87)

        review = create_review_for_identity_membership(membership)

        self.assertAlmostEqual(review.ai_confidence, 87.0, places=2)

    def test_a_perfect_identity_match_reads_as_one_hundred(self):
        membership = self.make_identity_membership(1.0)

        review = create_review_for_identity_membership(membership)

        self.assertAlmostEqual(review.ai_confidence, 100.0, places=2)

    def test_an_identity_match_with_no_score_stays_empty(self):
        membership = self.make_identity_membership(None)

        review = create_review_for_identity_membership(membership)

        self.assertIsNone(review.ai_confidence)

    def test_every_source_lands_in_the_same_range(self):
        quality_image = self.make_image(self.owner, self.organization)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=quality_image, status="completed",
        )
        quality = create_review_for_quality_decision(ImageQualityDecision.objects.create(
            owner=self.owner, image=quality_image, preprocessing_result=preprocessing,
            status="completed", decision=ImageQualityDecision.Decision.NEEDS_REVIEW,
            overall_score=61.0,
        ))
        identity = create_review_for_identity_membership(self.make_identity_membership(0.61))

        for review in (quality, identity):
            self.assertGreaterEqual(review.ai_confidence, 0)
            self.assertLessEqual(review.ai_confidence, 100)
        # Two sources that agree on how confident they are now read the same.
        self.assertAlmostEqual(quality.ai_confidence, identity.ai_confidence, places=2)

    def test_the_queue_serves_the_percentage_without_further_conversion(self):
        create_review_for_identity_membership(self.make_identity_membership(0.74))

        response = self.authenticated_client(self.owner).get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
        )

        rows = response.data["results"] if "results" in response.data else response.data
        identity_row = next(row for row in rows if row["source"] == "identity")
        self.assertAlmostEqual(identity_row["ai_confidence"], 74.0, places=2)

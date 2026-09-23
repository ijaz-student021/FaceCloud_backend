"""T-007 Phase C: a review is created automatically wherever an existing
pipeline already lands on an uncertain result -- these are additive hooks,
not rewrites of quality_control/duplicates/identities' own decision logic.
"""
from unittest.mock import MagicMock, patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase

from duplicates.models import DuplicateAnalysis, DuplicateMatch
from duplicates.services import _create_match
from identities.models import IdentityAnalysis
from identities.services import process_identity_analysis
from organizations.services import create_personal_organization
from quality_control.models import ImageQualityDecision
from quality_control.services import process_quality_decision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ModeratorReview

User = get_user_model()


class QualityReviewHookTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="quality-owner", email="quality-owner@example.com", password="Password!123",
        )
        self.organization = create_personal_organization(self.owner)
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single", organization=self.organization)
        self.image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="face.jpg", status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.image, status="completed",
        )
        self.decision = ImageQualityDecision.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.preprocessing, status="queued",
        )

    def _run(self, decision):
        metrics = {
            "mean_luminance": 128, "laplacian_sharpness": 100, "contrast_stddev": 50,
            "estimated_noise": 1, "shadow_clipping_percent": 0, "highlight_clipping_percent": 0,
            "width": 1000, "height": 1000, "defects": [],
        }
        with (
            patch("quality_control.services._read_standardized", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
            patch("quality_control.services.analyze_image_quality", return_value=metrics),
            patch(
                "quality_control.services.make_decision",
                return_value=(decision, [] if decision == "accepted" else ["borderline_blur"], "message"),
            ),
        ):
            process_quality_decision(self.decision.pk)

    def test_a_needs_review_quality_decision_creates_a_moderator_review(self):
        self._run("needs_review")

        review = ModeratorReview.objects.get(quality_decision=self.decision)
        self.assertEqual(review.source, ModeratorReview.Source.QUALITY)
        self.assertEqual(review.organization, self.organization)
        self.assertEqual(review.status, ModeratorReview.Status.PENDING)

    def test_an_accepted_quality_decision_creates_no_review(self):
        self._run("accepted")

        self.assertFalse(ModeratorReview.objects.filter(quality_decision=self.decision).exists())


class DuplicateReviewHookTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="dup-owner", email="dup-owner@example.com", password="Password!123",
        )
        self.organization = create_personal_organization(self.owner)
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple", organization=self.organization)
        self.source_image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="a.jpg", status="uploaded",
        )
        self.candidate = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="b.jpg", status="uploaded",
        )
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.source_image, status="completed",
        )
        self.analysis = DuplicateAnalysis.objects.create(
            owner=self.owner, image=self.source_image, preprocessing_result=preprocessing, status="processing",
        )

    def test_a_suspected_match_creates_a_moderator_review(self):
        _create_match(
            analysis=self.analysis, candidate=self.candidate, match_type=DuplicateMatch.MatchType.PERCEPTUAL,
            similarity_score=0.7, confirmed=False,
        )

        match = DuplicateMatch.objects.get()
        review = ModeratorReview.objects.get(duplicate_match=match)
        self.assertEqual(review.source, ModeratorReview.Source.DUPLICATE)
        self.assertEqual(review.organization, self.organization)

    def test_a_confirmed_exact_match_creates_no_review(self):
        _create_match(
            analysis=self.analysis, candidate=self.candidate, match_type=DuplicateMatch.MatchType.EXACT,
            similarity_score=1.0, confirmed=True,
        )

        self.assertFalse(ModeratorReview.objects.filter(duplicate_match__isnull=False).exists())


def _cosine_similarity(first, second):
    return float(np.dot(np.asarray(first, dtype=np.float32), np.asarray(second, dtype=np.float32)))


class IdentityReviewHookTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="identity-owner", email="identity-owner@example.com", password="Password!123",
        )
        self.organization = create_personal_organization(self.owner)
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single", organization=self.organization)
        self.image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="face.jpg", status="uploaded",
        )
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.image, status="completed",
        )
        validation = FaceValidationResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1,
            faces=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        )
        self.analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            face_validation=validation, status="queued", provider="test",
            policy_version="test-v1", task_id="task-1",
        )

    def test_an_ambiguous_match_creates_a_moderator_review(self):
        provider = MagicMock()
        provider.name = "test"
        provider.dimension = 3
        provider.model_checksum = "stub"
        provider.healthcheck.return_value = None
        provider.prepare_face.side_effect = lambda image, box, landmarks: image
        provider.embed.return_value = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        provider.validate_embedding.side_effect = lambda value: value
        provider.similarity.side_effect = _cosine_similarity

        with (
            patch("identities.services.get_identity_provider", return_value=provider),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
            patch("identities.services._choose_group", return_value=("review", None, 0.7, 0.66)),
        ):
            process_identity_analysis(self.analysis.pk)

        review = ModeratorReview.objects.get(source=ModeratorReview.Source.IDENTITY)
        self.assertEqual(review.organization, self.organization)
        self.assertEqual(review.image, self.image)

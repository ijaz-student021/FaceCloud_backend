"""A personal workspace has no moderator, so no review is opened there, it is left out of the administrator's moderation overview, and the ones that exist can be closed (BE-006)."""
import io
from datetime import timedelta
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from duplicates.models import DuplicateAnalysis, DuplicateMatch
from duplicates.services import _create_match
from identities.models import IdentityAnalysis, IdentityFace, IdentityMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from quality_control.services import process_quality_decision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .management.commands.close_personal_reviews import REASON
from .models import ModeratorReview
from .services import create_review_for_identity_membership, flag_overdue_reviews

User = get_user_model()


class Fixtures:
    def setUp(self):
        self.person = User.objects.create_user(username="solo-person", email="solo-person@example.com", password="Password!123", is_email_verified=True)
        self.personal = create_personal_organization(self.person)
        self.boss = User.objects.create_user(username="team-boss", email="team-boss@example.com", password="Password!123", is_email_verified=True)
        self.team = create_organization(name="Team Co", owner=self.boss)
        self.admin = User.objects.create_user(username="sys-admin", email="sys-admin@example.com", password="Password!123", is_email_verified=True, role="admin")
        self.count = 0

    def photo(self, organization, owner=None):
        self.count += 1
        owner = owner or (self.person if organization == self.personal else self.boss)
        batch = UploadBatch.objects.create(owner=owner, upload_type="multiple", organization=organization)
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=f"p{self.count}.jpg", status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        return image, preprocessing

    def review(self, organization, source="quality", status="pending", **extra):
        image, preprocessing = self.photo(organization)
        decision = ImageQualityDecision.objects.create(owner=image.owner, image=image, preprocessing_result=preprocessing, status="completed", decision="needs_review")
        return ModeratorReview.objects.create(
            organization=organization, image=image, source=source, status=status, quality_decision=decision, due_at=timezone.now() + timedelta(hours=24), **extra,
        )


class NoReviewIsOpenedTests(Fixtures, TestCase):
    def quality(self, organization):
        image, preprocessing = self.photo(organization)
        decision = ImageQualityDecision.objects.create(owner=image.owner, image=image, preprocessing_result=preprocessing, status="queued")
        metrics = {"mean_luminance": 128, "laplacian_sharpness": 100, "contrast_stddev": 50, "estimated_noise": 1, "shadow_clipping_percent": 0, "highlight_clipping_percent": 0,
                   "width": 1000, "height": 1000, "defects": []}
        with (
            patch("quality_control.services._read_standardized", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
            patch("quality_control.services.analyze_image_quality", return_value=metrics),
            patch("quality_control.services.make_decision", return_value=("needs_review", ["borderline_blur"], "message")),
        ):
            process_quality_decision(decision.pk)
        return decision

    def test_a_quality_doubt_in_a_personal_workspace_opens_no_review_but_the_same_in_a_team_does(self):
        mine = self.quality(self.personal)
        self.assertFalse(ModeratorReview.objects.filter(quality_decision=mine).exists())
        mine.refresh_from_db()
        self.assertEqual(mine.decision, "needs_review")                                  # what the checks found is still on record
        theirs = self.quality(self.team)
        self.assertTrue(ModeratorReview.objects.filter(quality_decision=theirs, organization=self.team).exists())

    def duplicate(self, organization):
        image, preprocessing = self.photo(organization)
        other, _ = self.photo(organization, owner=image.owner)
        analysis = DuplicateAnalysis.objects.create(owner=image.owner, image=image, preprocessing_result=preprocessing, status="processing")
        _create_match(analysis=analysis, candidate=other, match_type=DuplicateMatch.MatchType.PERCEPTUAL, similarity_score=0.7, confirmed=False)
        return DuplicateMatch.objects.get(analysis=analysis)

    def test_a_suspected_duplicate_opens_a_review_only_in_a_team(self):
        self.assertFalse(ModeratorReview.objects.filter(duplicate_match=self.duplicate(self.personal)).exists())
        self.assertTrue(ModeratorReview.objects.filter(duplicate_match=self.duplicate(self.team)).exists())

    def membership(self, organization):
        image, preprocessing = self.photo(organization)
        validation = FaceValidationResult.objects.create(owner=image.owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}])
        analysis = IdentityAnalysis.objects.create(owner=image.owner, image=image, preprocessing_result=preprocessing, face_validation=validation, status="queued", provider="test", policy_version="v1")
        face = IdentityFace.objects.create(owner=image.owner, image=image, analysis=analysis, face_index=0, protected_embedding=b"x", embedding_dimension=3, provider="test")
        return IdentityMembership.objects.create(owner=image.owner, identity_face=face, identity_group=None, state="needs_review", similarity_score=0.7, decision_method="test")

    def test_an_ambiguous_face_opens_a_review_only_in_a_team(self):
        self.assertIsNone(create_review_for_identity_membership(self.membership(self.personal)))
        self.assertIsNotNone(create_review_for_identity_membership(self.membership(self.team)))
        self.assertEqual(ModeratorReview.objects.filter(source="identity").count(), 1)

    def test_a_photograph_in_no_organization_still_opens_none(self):
        self.assertIsNone(create_review_for_identity_membership(self.membership(None)))


class OverviewTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = "/api/system/moderation/"

    def test_personal_workspaces_are_left_out_of_every_figure_and_a_team_is_not(self):
        for _ in range(4):
            self.review(self.personal)
        self.review(self.personal, status="in_review")
        self.review(self.team)
        past_due = self.review(self.team)
        ModeratorReview.objects.filter(pk=past_due.pk).update(due_at=timezone.now() - timedelta(hours=5), created_at=timezone.now() - timedelta(days=5))
        body = self.client.get(self.url).json()
        self.assertEqual(body["totals"]["pending"], 2)                                   # only the team's two
        self.assertEqual(body["totals"]["in_review"], 0)
        self.assertEqual(body["totals"]["unassigned"], 2)
        self.assertEqual(body["totals"]["overdue"], 1)
        self.assertEqual(body["totals"]["stale_open"], 1)
        self.assertEqual([row["organization_id"] for row in body["organizations_needing_attention"]], [self.team.pk])

    def test_with_only_personal_reviews_the_overview_is_empty(self):
        for _ in range(4):
            self.review(self.personal)
        body = self.client.get(self.url).json()
        self.assertEqual(body["organizations_needing_attention"], [])
        self.assertEqual((body["totals"]["pending"], body["totals"]["unassigned"], body["totals"]["overdue"], body["totals"]["stale_open"]), (0, 0, 0, 0))

    def test_a_moderator_who_decided_a_personal_review_is_not_counted_in_the_workload(self):
        moderator = User.objects.create_user(username="old-mod", email="old-mod@example.com", password="Password!123", role="moderator")
        done = self.review(self.personal, status="approved")
        ModeratorReview.objects.filter(pk=done.pk).update(decided_by=moderator, decided_at=timezone.now())
        self.assertEqual(self.client.get(self.url).json()["moderator_workload"], [])

    def test_an_overdue_personal_review_is_never_flagged(self):
        late = self.review(self.personal)
        ModeratorReview.objects.filter(pk=late.pk).update(due_at=timezone.now() - timedelta(hours=3))
        team_late = self.review(self.team)
        ModeratorReview.objects.filter(pk=team_late.pk).update(due_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(flag_overdue_reviews(), 1)
        self.assertIsNone(ModeratorReview.objects.get(pk=late.pk).sla_breached_at)
        self.assertIsNotNone(ModeratorReview.objects.get(pk=team_late.pk).sla_breached_at)


class CloseCommandTests(Fixtures, TestCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command("close_personal_reviews", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_says_what_it_would_close_and_changes_nothing(self):
        for _ in range(3):
            self.review(self.personal)
        self.review(self.personal, source="duplicate", status="escalated")
        text = self.run_command()
        self.assertIn("Dry run: 4 open review(s)", text)
        self.assertEqual(ModeratorReview.objects.filter(status__in=("pending", "escalated")).count(), 4)

    def test_apply_closes_them_with_the_reason_and_no_deciding_moderator(self):
        reviews = [self.review(self.personal) for _ in range(2)]
        claimed = self.review(self.personal, status="in_review", claimed_by=self.person, claimed_at=timezone.now())
        text = self.run_command("--apply")
        self.assertIn("Closed 3 review(s)", text)
        for review in [*reviews, claimed]:
            review.refresh_from_db()
            self.assertEqual((review.status, review.decision_reason, review.decided_by_id, review.claimed_by_id), ("rejected", REASON, None, None))
            self.assertIsNotNone(review.decided_at)
        self.assertEqual(REASON, "No moderator in a personal workspace")

    def test_a_team_review_and_an_already_decided_one_are_left_alone(self):
        team = self.review(self.team)
        decided = self.review(self.personal, status="approved")
        ModeratorReview.objects.filter(pk=decided.pk).update(decided_at=timezone.now() - timedelta(days=1), decided_by=self.boss, decision_reason="Looks fine")
        self.run_command("--apply")
        team.refresh_from_db()
        decided.refresh_from_db()
        self.assertEqual(team.status, "pending")
        self.assertEqual((decided.status, decided.decision_reason, decided.decided_by_id), ("approved", "Looks fine", self.boss.pk))

    def test_running_it_twice_is_safe(self):
        self.review(self.personal)
        first = self.run_command("--apply")
        second = self.run_command("--apply")
        self.assertIn("Closed 1 review(s)", first)
        self.assertIn("Closed 0 review(s)", second)

    def test_the_photograph_and_its_verdict_are_not_touched(self):
        review = self.review(self.personal)
        self.run_command("--apply")
        decision = ImageQualityDecision.objects.get(pk=review.quality_decision_id)
        self.assertEqual(decision.decision, "needs_review")
        self.assertTrue(ImageUpload.objects.filter(pk=review.image_id).exists())

    def test_after_closing_the_overview_shows_nothing_for_personal_workspaces(self):
        self.review(self.personal)
        self.run_command("--apply")
        client = APIClient()
        client.force_authenticate(self.admin)
        body = client.get("/api/system/moderation/").json()
        self.assertEqual((body["totals"]["pending"], body["organizations_needing_attention"]), (0, []))

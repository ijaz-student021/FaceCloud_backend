"""Two pieces an administrator's decision on a photograph shares with the moderator's (``operations/image_decision.py``): the verdict write-back, and reopening a decided review."""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision

from .models import ModeratorReview
from .services import apply_quality_verdict, decide_review, reopen_review
from .tests import ModerationFixtureMixin


class Base(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("reopen-owner")
        self.organization = create_organization(name="Reopen Co", owner=self.owner)
        self.moderator = self.make_user("reopen-moderator")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator, role="moderator", status="active")
        self.review, self.decision = self.make_quality_review(self.owner, self.organization)


class ApplyQualityVerdictTests(Base):
    def test_an_approval_writes_accepted_and_starts_the_enhancement_then_the_folders(self):
        before = self.decision.updated_at
        with patch("enhancement.auto.enhance_after_verdict") as enhance, patch("identities.verdict.group_when_enhancement_ends") as group:
            apply_quality_verdict(self.review.image, self.decision.pk, "approve")
        fresh = ImageQualityDecision.objects.get(pk=self.decision.pk)
        self.assertEqual(fresh.decision, "accepted")
        self.assertGreater(fresh.updated_at, before)
        enhance.assert_called_once_with(self.review.image, verdict="accepted")
        group.assert_called_once_with(self.review.image, verdict="accepted")

    def test_a_rejection_writes_rejected_and_takes_the_photo_out_of_its_folders_saying_who_did_it(self):
        with patch("identities.services.remove_photo_from_folders") as remove:
            apply_quality_verdict(self.review.image, self.decision.pk, "reject")
            apply_quality_verdict(self.review.image, self.decision.pk, "reject", by="an administrator")
        self.assertEqual(ImageQualityDecision.objects.get(pk=self.decision.pk).decision, "rejected")
        self.assertEqual([call.kwargs["reason"] for call in remove.call_args_list], ["The photo was rejected by a moderator.", "The photo was rejected by an administrator."])

    def test_a_moderators_decision_goes_through_it_with_the_moderators_wording(self):
        with patch("moderation.services.apply_quality_verdict") as verdict:
            decide_review(review=self.review, actor=self.moderator, action="reject", reason="blurry")
        verdict.assert_called_once()
        self.assertEqual(verdict.call_args.kwargs, {"by": "a moderator"})
        second_review, _decision = self.make_quality_review(self.owner, self.organization)
        with patch("moderation.services.apply_quality_verdict") as verdict:
            decide_review(review=second_review, actor=self.moderator, action="reject", reason="blurry", rejected_by="an administrator")
        self.assertEqual(verdict.call_args.kwargs, {"by": "an administrator"})

    def test_a_hold_or_a_correction_writes_nothing_back(self):
        with patch("moderation.services.apply_quality_verdict") as verdict:
            decide_review(review=self.review, actor=self.moderator, action="keep_in_review")
            decide_review(review=self.review, actor=self.moderator, action="correct")
        verdict.assert_not_called()
        self.assertEqual(ImageQualityDecision.objects.get(pk=self.decision.pk).decision, "needs_review")


class ReopenReviewTests(Base):
    def decided(self):
        decide_review(review=self.review, actor=self.moderator, action="reject", reason="blurry", notes="internal", message_to_member="Please retake")
        # It was decided late: past its deadline, which was reported.
        ModeratorReview.objects.filter(pk=self.review.pk).update(sla_breached_at=timezone.now(), due_at=timezone.now() - timezone.timedelta(days=5))
        return ModeratorReview.objects.get(pk=self.review.pk)

    def test_a_decided_review_is_pending_undecided_unassigned_and_unclaimed_again_with_a_fresh_deadline(self):
        decided = self.decided()
        self.assertEqual((decided.status, decided.decided_by_id), ("rejected", self.moderator.pk))
        reopened = reopen_review(review=decided, actor=self.owner, reason="Look again")
        fresh = ModeratorReview.objects.get(pk=self.review.pk)
        self.assertEqual(reopened.pk, fresh.pk)
        self.assertEqual((fresh.status, fresh.decided_by_id, fresh.decided_at, fresh.assigned_moderator_id), ("pending", None, None, None))
        self.assertEqual((fresh.claimed_by_id, fresh.claimed_at, fresh.sla_breached_at), (None, None, None))
        self.assertEqual((fresh.decision_reason, fresh.decision_notes, fresh.message_to_member), ("Look again", "", ""))
        self.assertGreater(fresh.due_at, timezone.now())
        self.assertTrue(fresh.is_open)
        self.assertEqual((fresh.source, fresh.quality_decision_id, fresh.ai_recommendation), ("quality", self.decision.pk, "needs_review"))      # what it is about is unchanged

    def test_it_is_written_to_the_organizations_audit_trail_next_to_the_decision_it_reverses(self):
        self.decided()
        reopen_review(review=self.review, actor=self.owner, reason="Look again")
        decided, reopened = OrganizationAuditEvent.objects.filter(action="moderator_review_decided").order_by("pk")
        self.assertEqual((decided.actor_id, decided.new_value["action"]), (self.moderator.pk, "reject"))
        self.assertEqual((reopened.actor_id, reopened.target_id, reopened.previous_value, reopened.reason), (self.owner.pk, self.review.pk, {"status": "rejected"}, "Look again"))
        self.assertEqual((reopened.new_value["action"], reopened.new_value["status"]), ("keep_in_review", "pending"))

    def test_an_open_review_is_returned_as_it_is_and_nothing_is_written(self):
        before = ModeratorReview.objects.get(pk=self.review.pk)
        returned = reopen_review(review=self.review, actor=self.owner, reason="Look again")
        after = ModeratorReview.objects.get(pk=self.review.pk)
        self.assertEqual((returned.pk, after.status, after.decision_reason, after.due_at, after.updated_at), (before.pk, "pending", "", before.due_at, before.updated_at))
        self.assertFalse(OrganizationAuditEvent.objects.filter(action="moderator_review_decided").exists())

    def test_it_can_be_decided_again_and_the_photograph_still_has_one_quality_review(self):
        self.decided()
        reopen_review(review=self.review, actor=self.owner)
        decide_review(review=self.review, actor=self.moderator, action="approve")
        self.assertEqual(ModeratorReview.objects.filter(image=self.review.image, source="quality").count(), 1)
        self.assertEqual(ModeratorReview.objects.get(pk=self.review.pk).status, "approved")

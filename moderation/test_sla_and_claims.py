"""T-009: review deadlines (SLA) and the claim soft lock.

Deadlines and claims both exist to stop queue work going wrong quietly -- a
review nobody notices is late, and two moderators spending their afternoon on
the same photo. The fixtures build ModeratorReview rows directly so each test
controls its own deadline; the one test that covers stamping goes through the
real create_review_for_quality_decision hook.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from notifications.models import UserNotification
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult

from .models import ModeratorReview
from .services import (
    ModerationConflict,
    claim_review,
    create_review_for_quality_decision,
    dashboard_summary,
    decide_review,
    escalate_review,
    flag_overdue_reviews,
    release_open_reviews,
    release_review_claim,
    review_deadline,
)
from .tests import ModerationFixtureMixin


class ReviewDeadlineStampingTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("sla-stamp-owner")
        self.organization = create_organization(name="Stamp Co", owner=self.owner)

    def make_uncertain_decision(self):
        image = self.make_image(self.owner, self.organization)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=image, status="completed",
        )
        return ImageQualityDecision.objects.create(
            owner=self.owner, image=image, preprocessing_result=preprocessing, status="completed",
            decision=ImageQualityDecision.Decision.NEEDS_REVIEW, overall_score=0.5,
        )

    @override_settings(MODERATION_SLA_HOURS=24)
    def test_a_review_created_by_the_hook_is_given_a_deadline(self):
        before = timezone.now()
        review = create_review_for_quality_decision(self.make_uncertain_decision())

        self.assertIsNotNone(review.due_at)
        self.assertGreater(review.due_at, before + timedelta(hours=23))
        self.assertLess(review.due_at, before + timedelta(hours=25))

    @override_settings(MODERATION_SLA_HOURS=4)
    def test_the_deadline_follows_the_configured_service_level(self):
        review = create_review_for_quality_decision(self.make_uncertain_decision())

        self.assertLess(review.due_at, timezone.now() + timedelta(hours=5))
        self.assertGreater(review.due_at, timezone.now() + timedelta(hours=3))

    @override_settings(MODERATION_SLA_HOURS=48)
    def test_changing_the_service_level_does_not_move_an_existing_deadline(self):
        review = create_review_for_quality_decision(self.make_uncertain_decision())
        original_due_at = review.due_at

        with override_settings(MODERATION_SLA_HOURS=1):
            review.refresh_from_db()

        self.assertEqual(review.due_at, original_due_at)

    def test_review_deadline_measures_from_the_time_it_is_given(self):
        anchor = timezone.now() - timedelta(days=5)

        with override_settings(MODERATION_SLA_HOURS=6):
            self.assertEqual(review_deadline(anchor), anchor + timedelta(hours=6))


class OverdueReviewTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("overdue-owner")
        self.organization = create_organization(name="Overdue Co", owner=self.owner)
        self.client = self.authenticated_client(self.owner)

    def make_review_due(self, due_at, **extra):
        review, _ = self.make_quality_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).update(due_at=due_at, **extra)
        review.refresh_from_db()
        return review

    def queue(self, **params):
        response = self.client.get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
            params,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"] if "results" in response.data else response.data
        return [row["id"] for row in rows]

    def test_an_undecided_review_past_its_deadline_is_overdue(self):
        late = self.make_review_due(timezone.now() - timedelta(hours=1))

        self.assertTrue(late.is_overdue())
        self.assertIn(late.pk, self.queue(overdue="true"))

    def test_a_review_still_inside_its_deadline_is_not_overdue(self):
        in_time = self.make_review_due(timezone.now() + timedelta(hours=5))

        self.assertFalse(in_time.is_overdue())
        self.assertNotIn(in_time.pk, self.queue(overdue="true"))

    def test_a_decided_review_is_never_overdue_however_late_the_decision_was(self):
        late = self.make_review_due(timezone.now() - timedelta(days=2))
        decide_review(review=late, actor=self.owner, action="approve")
        late.refresh_from_db()

        self.assertFalse(late.is_overdue())
        self.assertNotIn(late.pk, self.queue(overdue="true"))

    def test_a_review_without_a_deadline_is_never_overdue(self):
        review, _ = self.make_quality_review(self.owner, self.organization)

        self.assertIsNone(review.due_at)
        self.assertFalse(review.is_overdue())
        self.assertNotIn(review.pk, self.queue(overdue="true"))

    def test_the_serializer_reports_the_deadline_and_whether_it_has_passed(self):
        late = self.make_review_due(timezone.now() - timedelta(hours=3))

        response = self.client.get(reverse(
            "moderation:review-detail",
            kwargs={"organization_id": self.organization.pk, "review_id": late.pk},
        ))

        self.assertTrue(response.data["is_overdue"])
        self.assertIsNotNone(response.data["due_at"])

    def test_ordering_by_deadline_puts_the_soonest_first_and_undated_last(self):
        undated, _ = self.make_quality_review(self.owner, self.organization)
        later = self.make_review_due(timezone.now() + timedelta(hours=10))
        sooner = self.make_review_due(timezone.now() + timedelta(hours=2))

        ordered = self.queue(ordering="due_at")

        self.assertEqual(
            [sooner.pk, later.pk, undated.pk],
            [pk for pk in ordered if pk in {sooner.pk, later.pk, undated.pk}],
        )

    def test_the_overdue_filter_ignores_reviews_from_other_organizations(self):
        other_owner = self.make_user("overdue-other-owner")
        other_org = create_organization(name="Other Overdue Co", owner=other_owner)
        other_review, _ = self.make_quality_review(other_owner, other_org)
        ModeratorReview.objects.filter(pk=other_review.pk).update(
            due_at=timezone.now() - timedelta(days=1),
        )

        self.assertNotIn(other_review.pk, self.queue(overdue="true"))


@override_settings(MODERATION_SLA_HOURS=24, MODERATION_SLA_DUE_SOON_HOURS=4)
class DashboardServiceLevelTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("sla-dash-owner")
        self.organization = create_organization(name="Dash SLA Co", owner=self.owner)

    def make_review_due(self, due_at):
        review, _ = self.make_quality_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).update(due_at=due_at)
        review.refresh_from_db()
        return review

    def test_the_dashboard_counts_overdue_reviews(self):
        self.make_review_due(timezone.now() - timedelta(hours=2))
        self.make_review_due(timezone.now() + timedelta(hours=20))

        self.assertEqual(dashboard_summary(self.organization)["overdue"], 1)

    def test_due_soon_excludes_reviews_that_are_already_overdue(self):
        self.make_review_due(timezone.now() - timedelta(hours=2))
        self.make_review_due(timezone.now() + timedelta(hours=1))

        summary = dashboard_summary(self.organization)

        self.assertEqual(summary["overdue"], 1)
        self.assertEqual(summary["due_soon"], 1)

    def test_a_review_beyond_the_due_soon_window_is_in_neither_count(self):
        self.make_review_due(timezone.now() + timedelta(hours=20))

        summary = dashboard_summary(self.organization)

        self.assertEqual(summary["overdue"], 0)
        self.assertEqual(summary["due_soon"], 0)

    def test_the_dashboard_counts_reviews_nobody_has_been_given(self):
        unassigned = self.make_review_due(timezone.now() + timedelta(hours=10))
        assigned = self.make_review_due(timezone.now() + timedelta(hours=10))
        ModeratorReview.objects.filter(pk=assigned.pk).update(assigned_moderator=self.owner)

        self.assertEqual(dashboard_summary(self.organization)["unassigned"], 1)
        self.assertIsNone(unassigned.assigned_moderator)

    def test_the_dashboard_reports_how_long_the_oldest_open_review_has_waited(self):
        review = self.make_review_due(timezone.now() + timedelta(hours=10))
        ModeratorReview.objects.filter(pk=review.pk).update(
            created_at=timezone.now() - timedelta(hours=5),
        )

        age = dashboard_summary(self.organization)["oldest_open_age_hours"]

        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 5.0, delta=0.1)

    def test_the_oldest_open_age_is_null_when_nothing_is_waiting(self):
        self.assertIsNone(dashboard_summary(self.organization)["oldest_open_age_hours"])

    def test_the_dashboard_reports_the_configured_service_level(self):
        self.assertEqual(dashboard_summary(self.organization)["sla_hours"], 24)

    def test_the_dashboard_endpoint_serves_the_service_level_fields(self):
        self.make_review_due(timezone.now() - timedelta(hours=1))

        response = self.authenticated_client(self.owner).get(
            reverse("moderation:dashboard", kwargs={"organization_id": self.organization.pk}),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["overdue"], 1)
        self.assertEqual(response.data["sla_hours"], 24)


class OverdueNotificationTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("breach-owner")
        self.organization = create_organization(name="Breach Co", owner=self.owner)
        self.moderator = self.make_user("breach-moderator")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator,
            role=OrganizationMembership.Role.MODERATOR,
        )

    def make_late_review(self, **extra):
        review, _ = self.make_quality_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).update(
            due_at=timezone.now() - timedelta(hours=2), **extra,
        )
        review.refresh_from_db()
        return review

    def overdue_notifications(self, recipient):
        return UserNotification.objects.filter(
            recipient=recipient, event_type=UserNotification.EventType.MODERATOR_REVIEW_OVERDUE,
        )

    def test_the_assigned_moderator_is_told_their_review_is_late(self):
        review = self.make_late_review(assigned_moderator=self.moderator)

        self.assertEqual(flag_overdue_reviews(), 1)
        self.assertEqual(self.overdue_notifications(self.moderator).count(), 1)
        review.refresh_from_db()
        self.assertIsNotNone(review.sla_breached_at)

    def test_a_late_review_is_reported_once_and_not_again(self):
        self.make_late_review(assigned_moderator=self.moderator)

        self.assertEqual(flag_overdue_reviews(), 1)
        self.assertEqual(flag_overdue_reviews(), 0)
        self.assertEqual(self.overdue_notifications(self.moderator).count(), 1)

    def test_an_unassigned_late_review_reaches_the_owners_and_moderators(self):
        self.make_late_review()

        self.assertEqual(flag_overdue_reviews(), 1)
        self.assertEqual(self.overdue_notifications(self.owner).count(), 1)
        self.assertEqual(self.overdue_notifications(self.moderator).count(), 1)

    def test_a_review_inside_its_deadline_is_not_reported(self):
        review, _ = self.make_quality_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).update(
            due_at=timezone.now() + timedelta(hours=5), assigned_moderator=self.moderator,
        )

        self.assertEqual(flag_overdue_reviews(), 0)
        self.assertEqual(self.overdue_notifications(self.moderator).count(), 0)

    def test_a_decided_review_is_not_reported_even_when_it_was_late(self):
        review = self.make_late_review(assigned_moderator=self.moderator)
        decide_review(review=review, actor=self.moderator, action="approve")

        self.assertEqual(flag_overdue_reviews(), 0)
        self.assertEqual(self.overdue_notifications(self.moderator).count(), 0)

    def test_a_review_without_a_deadline_is_not_reported(self):
        review, _ = self.make_quality_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).update(assigned_moderator=self.moderator)

        self.assertEqual(flag_overdue_reviews(), 0)


@override_settings(MODERATION_CLAIM_MINUTES=15)
class ReviewClaimTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("claim-owner")
        self.organization = create_organization(name="Claim Co", owner=self.owner)
        self.first = self.make_moderator("claim-mod-one")
        self.second = self.make_moderator("claim-mod-two")
        self.review, self.decision = self.make_quality_review(self.owner, self.organization)

    def make_moderator(self, name):
        user = self.make_user(name)
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=OrganizationMembership.Role.MODERATOR,
        )
        return user

    def claim_url(self, review=None):
        return reverse("moderation:review-claim", kwargs={
            "organization_id": self.organization.pk, "review_id": (review or self.review).pk,
        })

    def release_url(self, review=None):
        return reverse("moderation:review-release", kwargs={
            "organization_id": self.organization.pk, "review_id": (review or self.review).pk,
        })

    def expire_claim(self, review):
        ModeratorReview.objects.filter(pk=review.pk).update(
            claimed_at=timezone.now() - timedelta(minutes=16),
        )

    def test_claiming_a_pending_review_records_the_holder_and_opens_it(self):
        response = self.authenticated_client(self.first).post(self.claim_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertEqual(self.review.claimed_by_id, self.first.pk)
        self.assertEqual(self.review.status, ModeratorReview.Status.IN_REVIEW)
        self.assertEqual(self.review.assigned_moderator_id, self.first.pk)

    def test_a_claim_is_recorded_in_the_audit_trail(self):
        self.authenticated_client(self.first).post(self.claim_url())

        self.assertTrue(OrganizationAuditEvent.objects.filter(
            organization=self.organization, actor=self.first,
            action=OrganizationAuditEvent.Action.MODERATOR_REVIEW_CLAIMED,
            target_id=self.review.pk,
        ).exists())

    def test_a_second_moderator_cannot_claim_a_held_review(self):
        claim_review(review=self.review, actor=self.first)

        response = self.authenticated_client(self.second).post(self.claim_url())

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.review.refresh_from_db()
        self.assertEqual(self.review.claimed_by_id, self.first.pk)

    def test_the_holder_can_claim_again_to_extend_the_hold(self):
        claim_review(review=self.review, actor=self.first)
        self.review.refresh_from_db()
        first_expiry = self.review.claim_expires_at
        ModeratorReview.objects.filter(pk=self.review.pk).update(
            claimed_at=timezone.now() - timedelta(minutes=10),
        )

        response = self.authenticated_client(self.first).post(self.claim_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertGreater(self.review.claim_expires_at, first_expiry - timedelta(minutes=1))

    def test_an_expired_claim_lets_another_moderator_take_the_review(self):
        claim_review(review=self.review, actor=self.first)
        self.expire_claim(self.review)

        response = self.authenticated_client(self.second).post(self.claim_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertEqual(self.review.claimed_by_id, self.second.pk)

    def test_an_expired_claim_reads_as_free_in_the_queue(self):
        claim_review(review=self.review, actor=self.first)
        self.expire_claim(self.review)
        self.review.refresh_from_db()

        self.assertFalse(self.review.is_claimed())
        self.assertIsNone(self.review.active_claim_expiry())

    def test_another_moderator_cannot_decide_a_review_somebody_else_holds(self):
        claim_review(review=self.review, actor=self.first)

        response = self.authenticated_client(self.second).post(
            reverse("moderation:review-decision", kwargs={
                "organization_id": self.organization.pk, "review_id": self.review.pk,
            }),
            {"action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.decided_at)

    def test_another_moderator_cannot_escalate_a_review_somebody_else_holds(self):
        claim_review(review=self.review, actor=self.first)

        with self.assertRaises(ModerationConflict):
            escalate_review(review=self.review, actor=self.second)

    def test_the_holder_can_decide_their_own_claimed_review(self):
        claim_review(review=self.review, actor=self.first)

        decided = decide_review(review=self.review, actor=self.first, action="approve")

        self.assertEqual(decided.status, ModeratorReview.Status.APPROVED)

    def test_once_a_claim_expires_another_moderator_can_decide(self):
        claim_review(review=self.review, actor=self.first)
        self.expire_claim(self.review)
        self.review.refresh_from_db()

        decided = decide_review(review=self.review, actor=self.second, action="approve")

        self.assertEqual(decided.status, ModeratorReview.Status.APPROVED)

    def test_deciding_a_review_clears_the_claim(self):
        claim_review(review=self.review, actor=self.first)

        decided = decide_review(review=self.review, actor=self.first, action="approve")

        self.assertIsNone(decided.claimed_by_id)
        self.assertIsNone(decided.claimed_at)

    def test_holding_a_review_keeps_the_claim(self):
        claim_review(review=self.review, actor=self.first)

        held = decide_review(review=self.review, actor=self.first, action="keep_in_review")

        self.assertEqual(held.claimed_by_id, self.first.pk)

    def test_escalating_a_review_clears_the_claim(self):
        claim_review(review=self.review, actor=self.first)

        escalated = escalate_review(review=self.review, actor=self.first)

        self.assertIsNone(escalated.claimed_by_id)

    def test_claiming_an_escalated_review_keeps_it_escalated(self):
        escalate_review(review=self.review, actor=self.first)
        self.review.refresh_from_db()

        claimed = claim_review(review=self.review, actor=self.owner)

        self.assertEqual(claimed.status, ModeratorReview.Status.ESCALATED)
        self.assertEqual(claimed.claimed_by_id, self.owner.pk)

    def test_a_decided_review_cannot_be_claimed(self):
        decide_review(review=self.review, actor=self.first, action="approve")
        self.review.refresh_from_db()

        with self.assertRaises(ModerationConflict):
            claim_review(review=self.review, actor=self.second)

    def test_the_holder_can_release_the_review_back_to_the_queue(self):
        claim_review(review=self.review, actor=self.first)

        response = self.authenticated_client(self.first).post(self.release_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.claimed_by_id)
        self.assertEqual(self.review.status, ModeratorReview.Status.PENDING)

    def test_another_moderator_cannot_release_somebody_elses_claim(self):
        claim_review(review=self.review, actor=self.first)

        response = self.authenticated_client(self.second).post(self.release_url())

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.review.refresh_from_db()
        self.assertEqual(self.review.claimed_by_id, self.first.pk)

    def test_an_owner_can_release_a_claim_held_by_a_moderator(self):
        claim_review(review=self.review, actor=self.first)

        response = self.authenticated_client(self.owner).post(self.release_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.claimed_by_id)

    def test_releasing_an_unclaimed_review_is_a_conflict_not_a_silent_success(self):
        response = self.authenticated_client(self.first).post(self.release_url())

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_release_is_recorded_in_the_audit_trail(self):
        claim_review(review=self.review, actor=self.first)
        release_review_claim(review=self.review, actor=self.first)

        self.assertTrue(OrganizationAuditEvent.objects.filter(
            organization=self.organization, actor=self.first,
            action=OrganizationAuditEvent.Action.MODERATOR_REVIEW_RELEASED,
            target_id=self.review.pk,
        ).exists())

    def test_a_plain_member_cannot_claim_a_review(self):
        member = self.make_user("claim-plain-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.authenticated_client(member).post(self.claim_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_losing_moderator_rights_drops_the_claim_as_well_as_the_assignment(self):
        claim_review(review=self.review, actor=self.first)

        release_open_reviews(organization=self.organization, user=self.first)

        self.review.refresh_from_db()
        self.assertIsNone(self.review.claimed_by_id)
        self.assertIsNone(self.review.claimed_at)
        self.assertIsNone(self.review.assigned_moderator_id)
        self.assertEqual(self.review.status, ModeratorReview.Status.PENDING)

    def test_a_claim_on_someone_elses_review_is_dropped_too(self):
        ModeratorReview.objects.filter(pk=self.review.pk).update(assigned_moderator=self.second)
        self.review.refresh_from_db()
        ModeratorReview.objects.filter(pk=self.review.pk).update(
            claimed_by=self.first, claimed_at=timezone.now(),
        )

        release_open_reviews(organization=self.organization, user=self.first)

        self.review.refresh_from_db()
        self.assertIsNone(self.review.claimed_by_id)
        self.assertEqual(self.review.assigned_moderator_id, self.second.pk)

    def test_the_serializer_reports_the_holder_and_when_the_hold_ends(self):
        claim_review(review=self.review, actor=self.first)

        response = self.authenticated_client(self.first).get(reverse(
            "moderation:review-detail",
            kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
        ))

        self.assertTrue(response.data["is_claimed"])
        self.assertEqual(response.data["claimed_by"]["username"], self.first.username)
        self.assertIsNotNone(response.data["claim_expires_at"])

"""T-009: bulk decisions, and per-moderator performance metrics.

A bulk decision is deliberately not one transaction: a moderator who selected
thirty photos should not lose twenty-nine of them because one was already
decided elsewhere, so each row succeeds or fails on its own and the response
says which. The metrics tests pin down the arithmetic -- particularly that a
review with no deadline counts as neither on time nor late.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError

from duplicates.models import DuplicateMatch
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision

from .models import ModeratorReview
from .services import bulk_decide_reviews, claim_review, decide_review, moderator_metrics
from .tests import ModerationFixtureMixin


class BulkDecisionMixin(ModerationFixtureMixin):
    def make_moderator(self, name):
        user = self.make_user(name)
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=OrganizationMembership.Role.MODERATOR,
        )
        return user

    def bulk_url(self, organization=None):
        return reverse("moderation:review-bulk-decision", kwargs={
            "organization_id": (organization or self.organization).pk,
        })


class BulkDecisionTests(BulkDecisionMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("bulk-owner")
        self.organization = create_organization(name="Bulk Co", owner=self.owner)
        self.client = self.authenticated_client(self.owner)
        self.first, self.first_decision = self.make_quality_review(self.owner, self.organization)
        self.second, self.second_decision = self.make_quality_review(self.owner, self.organization)

    def test_bulk_approving_decides_every_selected_review(self):
        response = self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "approve"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["succeeded_count"], 2)
        self.assertEqual(response.data["failed_count"], 0)
        for review in (self.first, self.second):
            review.refresh_from_db()
            self.assertEqual(review.status, ModeratorReview.Status.APPROVED)

    def test_bulk_approving_writes_back_to_the_underlying_quality_decisions(self):
        self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "approve"},
            format="json",
        )

        for decision in (self.first_decision, self.second_decision):
            decision.refresh_from_db()
            self.assertEqual(decision.decision, ImageQualityDecision.Decision.ACCEPTED)

    def test_bulk_rejecting_writes_back_to_the_underlying_quality_decisions(self):
        self.client.post(
            self.bulk_url(), {"review_ids": [self.first.pk], "action": "reject"}, format="json",
        )

        self.first_decision.refresh_from_db()
        self.assertEqual(self.first_decision.decision, ImageQualityDecision.Decision.REJECTED)

    def test_bulk_escalating_escalates_every_selected_review(self):
        response = self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "escalate", "reason": "unclear"},
            format="json",
        )

        self.assertEqual(response.data["succeeded_count"], 2)
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, ModeratorReview.Status.ESCALATED)

    def test_the_decision_reason_is_applied_to_each_review(self):
        self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk], "action": "reject", "reason": "blurred beyond use"},
            format="json",
        )

        self.first.refresh_from_db()
        self.assertEqual(self.first.decision_reason, "blurred beyond use")

    def test_an_already_decided_review_fails_without_taking_the_others_down(self):
        decide_review(review=self.first, actor=self.owner, action="approve")

        response = self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "reject"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["succeeded"], [self.second.pk])
        self.assertEqual(response.data["failed_count"], 1)
        self.assertEqual(response.data["failed"][0]["review_id"], self.first.pk)
        self.second.refresh_from_db()
        self.assertEqual(self.second.status, ModeratorReview.Status.REJECTED)

    def test_a_review_held_by_another_moderator_fails_and_is_left_alone(self):
        other = self.make_moderator("bulk-other-moderator")
        claim_review(review=self.first, actor=other)

        response = self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "approve"},
            format="json",
        )

        self.assertEqual(response.data["succeeded"], [self.second.pk])
        self.assertEqual(response.data["failed_count"], 1)
        self.first.refresh_from_db()
        self.assertIsNone(self.first.decided_at)

    def test_a_review_from_another_organization_is_reported_not_a_404(self):
        other_owner = self.make_user("bulk-other-owner")
        other_org = create_organization(name="Bulk Other Co", owner=other_owner)
        foreign, _ = self.make_quality_review(other_owner, other_org)

        response = self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, foreign.pk], "action": "approve"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["succeeded"], [self.first.pk])
        self.assertEqual(response.data["failed"][0]["review_id"], foreign.pk)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, ModeratorReview.Status.PENDING)

    def test_a_review_that_does_not_exist_is_reported_as_a_failure(self):
        response = self.client.post(
            self.bulk_url(), {"review_ids": [self.first.pk, 9_999_999], "action": "approve"},
            format="json",
        )

        self.assertEqual(response.data["succeeded_count"], 1)
        self.assertEqual(response.data["failed"][0]["review_id"], 9_999_999)

    def test_a_repeated_review_id_is_only_decided_once(self):
        response = self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.first.pk], "action": "approve"},
            format="json",
        )

        self.assertEqual(response.data["requested"], 1)
        self.assertEqual(response.data["succeeded"], [self.first.pk])
        self.assertEqual(response.data["failed_count"], 0)

    def test_correcting_is_not_offered_as_a_bulk_action(self):
        response = self.client.post(
            self.bulk_url(), {"review_ids": [self.first.pk], "action": "correct"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, ModeratorReview.Status.PENDING)

    def test_an_empty_selection_is_rejected(self):
        response = self.client.post(
            self.bulk_url(), {"review_ids": [], "action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(MODERATION_BULK_MAX_REVIEWS=2)
    def test_a_selection_over_the_limit_is_rejected(self):
        response = self.client.post(
            self.bulk_url(), {"review_ids": [1, 2, 3], "action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_bulk_decision_is_recorded_as_one_summary_audit_event(self):
        self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "approve"},
            format="json",
        )

        summary = OrganizationAuditEvent.objects.filter(
            organization=self.organization,
            action=OrganizationAuditEvent.Action.MODERATOR_BULK_DECISION,
        ).first()

        self.assertIsNotNone(summary)
        self.assertEqual(summary.new_value["requested"], 2)
        self.assertEqual(summary.new_value["action"], "approve")

    def test_each_review_still_gets_its_own_decision_audit_event(self):
        self.client.post(
            self.bulk_url(),
            {"review_ids": [self.first.pk, self.second.pk], "action": "approve"},
            format="json",
        )

        self.assertEqual(
            OrganizationAuditEvent.objects.filter(
                organization=self.organization,
                action=OrganizationAuditEvent.Action.MODERATOR_REVIEW_DECIDED,
            ).count(),
            2,
        )

    def test_a_plain_member_cannot_decide_in_bulk(self):
        member = self.make_user("bulk-plain-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.authenticated_client(member).post(
            self.bulk_url(), {"review_ids": [self.first.pk], "action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_unrelated_user_cannot_reach_the_endpoint(self):
        response = self.authenticated_client(self.make_user("bulk-outsider")).post(
            self.bulk_url(), {"review_ids": [self.first.pk], "action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_moderator_can_decide_in_bulk(self):
        moderator = self.make_moderator("bulk-moderator")

        response = self.authenticated_client(moderator).post(
            self.bulk_url(), {"review_ids": [self.first.pk], "action": "approve"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.first.refresh_from_db()
        self.assertEqual(self.first.decided_by_id, moderator.pk)

    def test_bulk_rejecting_a_duplicate_review_writes_back_to_the_match(self):
        review, match = self.make_duplicate_review(self.owner, self.organization)

        self.client.post(
            self.bulk_url(), {"review_ids": [review.pk], "action": "reject"}, format="json",
        )

        match.refresh_from_db()
        self.assertEqual(match.review_status, DuplicateMatch.ReviewStatus.NOT_DUPLICATE)

    def test_an_unknown_action_is_refused_by_the_service(self):
        with self.assertRaises(ValidationError):
            bulk_decide_reviews(
                organization=self.organization, actor=self.owner,
                review_ids=[self.first.pk], action="correct",
            )

    def test_an_empty_selection_is_refused_by_the_service(self):
        with self.assertRaises(ValidationError):
            bulk_decide_reviews(
                organization=self.organization, actor=self.owner,
                review_ids=[], action="approve",
            )


@override_settings(MODERATION_SLA_HOURS=24)
class ModeratorPerformanceTests(BulkDecisionMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("metrics-owner")
        self.organization = create_organization(name="Metrics Co", owner=self.owner)
        self.moderator = self.make_moderator("metrics-moderator")
        self.client = self.authenticated_client(self.owner)

    def metrics_url(self):
        return reverse("moderation:performance-metrics", kwargs={
            "organization_id": self.organization.pk,
        })

    def decided_review(self, *, actor, action="approve", created_hours_ago=5, took_hours=1, due_in_hours=24):
        """A review created some hours ago and decided some hours after that."""
        review, _ = self.make_quality_review(self.owner, self.organization)
        created_at = timezone.now() - timedelta(hours=created_hours_ago)
        ModeratorReview.objects.filter(pk=review.pk).update(
            created_at=created_at,
            due_at=created_at + timedelta(hours=due_in_hours) if due_in_hours is not None else None,
        )
        review.refresh_from_db()
        decide_review(review=review, actor=actor, action=action)
        ModeratorReview.objects.filter(pk=review.pk).update(
            decided_at=created_at + timedelta(hours=took_hours),
        )
        review.refresh_from_db()
        return review

    def row_for(self, metrics, user):
        return next(row for row in metrics["moderators"] if row["moderator_id"] == user.pk)

    def test_a_moderators_decisions_are_counted_by_outcome(self):
        self.decided_review(actor=self.moderator, action="approve")
        self.decided_review(actor=self.moderator, action="approve")
        self.decided_review(actor=self.moderator, action="reject")

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertEqual(row["decided_count"], 3)
        self.assertEqual(row["approved"], 2)
        self.assertEqual(row["rejected"], 1)

    def test_each_moderator_is_reported_separately(self):
        second = self.make_moderator("metrics-moderator-two")
        self.decided_review(actor=self.moderator)
        self.decided_review(actor=second)
        self.decided_review(actor=second)

        metrics = moderator_metrics(self.organization)

        self.assertEqual(self.row_for(metrics, self.moderator)["decided_count"], 1)
        self.assertEqual(self.row_for(metrics, second)["decided_count"], 2)

    def test_the_busiest_moderator_is_reported_first(self):
        second = self.make_moderator("metrics-busier")
        self.decided_review(actor=self.moderator)
        self.decided_review(actor=second)
        self.decided_review(actor=second)

        metrics = moderator_metrics(self.organization)

        self.assertEqual(metrics["moderators"][0]["moderator_id"], second.pk)

    def test_the_average_decision_time_is_measured_from_when_the_review_arrived(self):
        self.decided_review(actor=self.moderator, created_hours_ago=10, took_hours=2)
        self.decided_review(actor=self.moderator, created_hours_ago=10, took_hours=4)

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertAlmostEqual(row["average_decision_hours"], 3.0, delta=0.05)

    def test_the_slowest_decision_is_reported_alongside_the_average(self):
        self.decided_review(actor=self.moderator, created_hours_ago=20, took_hours=2)
        self.decided_review(actor=self.moderator, created_hours_ago=20, took_hours=9)

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertAlmostEqual(row["slowest_decision_hours"], 9.0, delta=0.05)

    def test_a_decision_inside_the_deadline_counts_as_on_time(self):
        self.decided_review(actor=self.moderator, created_hours_ago=30, took_hours=2, due_in_hours=24)

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertEqual(row["on_time"], 1)
        self.assertEqual(row["late"], 0)
        self.assertEqual(row["on_time_rate"], 1.0)

    def test_a_decision_after_the_deadline_counts_as_late(self):
        self.decided_review(actor=self.moderator, created_hours_ago=60, took_hours=40, due_in_hours=24)

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertEqual(row["on_time"], 0)
        self.assertEqual(row["late"], 1)
        self.assertEqual(row["on_time_rate"], 0.0)

    def test_the_on_time_rate_mixes_both_outcomes(self):
        self.decided_review(actor=self.moderator, created_hours_ago=60, took_hours=1, due_in_hours=24)
        self.decided_review(actor=self.moderator, created_hours_ago=60, took_hours=2, due_in_hours=24)
        self.decided_review(actor=self.moderator, created_hours_ago=60, took_hours=40, due_in_hours=24)

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertEqual(row["on_time"], 2)
        self.assertEqual(row["late"], 1)
        self.assertAlmostEqual(row["on_time_rate"], 0.6667, delta=0.0001)

    def test_a_review_without_a_deadline_counts_as_neither_on_time_nor_late(self):
        self.decided_review(actor=self.moderator, due_in_hours=None)

        row = self.row_for(moderator_metrics(self.organization), self.moderator)

        self.assertEqual(row["decided_count"], 1)
        self.assertEqual(row["on_time"], 0)
        self.assertEqual(row["late"], 0)
        self.assertIsNone(row["on_time_rate"])

    def test_decisions_older_than_the_window_are_excluded(self):
        old = self.decided_review(actor=self.moderator)
        ModeratorReview.objects.filter(pk=old.pk).update(
            decided_at=timezone.now() - timedelta(days=40),
        )

        self.assertEqual(moderator_metrics(self.organization, days=30)["totals"]["decided_count"], 0)
        self.assertEqual(moderator_metrics(self.organization, days=90)["totals"]["decided_count"], 1)

    def test_an_undecided_review_contributes_nothing_but_counts_as_open(self):
        self.make_quality_review(self.owner, self.organization)

        totals = moderator_metrics(self.organization)["totals"]

        self.assertEqual(totals["decided_count"], 0)
        self.assertEqual(totals["open_now"], 1)

    def test_the_totals_report_what_is_overdue_right_now(self):
        review, _ = self.make_quality_review(self.owner, self.organization)
        ModeratorReview.objects.filter(pk=review.pk).update(
            due_at=timezone.now() - timedelta(hours=2),
        )

        self.assertEqual(moderator_metrics(self.organization)["totals"]["overdue_now"], 1)

    def test_another_organizations_decisions_are_not_counted(self):
        other_owner = self.make_user("metrics-other-owner")
        other_org = create_organization(name="Metrics Other Co", owner=other_owner)
        foreign, _ = self.make_quality_review(other_owner, other_org)
        decide_review(review=foreign, actor=other_owner, action="approve")

        self.assertEqual(moderator_metrics(self.organization)["totals"]["decided_count"], 0)

    def test_the_endpoint_serves_the_metrics_to_an_owner(self):
        self.decided_review(actor=self.moderator)

        response = self.client.get(self.metrics_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["window_days"], 30)
        self.assertEqual(response.data["totals"]["decided_count"], 1)

    def test_the_window_can_be_narrowed_with_the_days_parameter(self):
        response = self.client.get(self.metrics_url(), {"days": 7})

        self.assertEqual(response.data["window_days"], 7)

    def test_a_days_value_that_is_not_a_number_is_rejected(self):
        response = self.client.get(self.metrics_url(), {"days": "last-week"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_days_value_of_zero_is_rejected(self):
        response = self.client.get(self.metrics_url(), {"days": 0})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(MODERATION_METRICS_MAX_DAYS=90)
    def test_a_days_value_beyond_the_maximum_is_rejected(self):
        response = self.client.get(self.metrics_url(), {"days": 91})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_empty_days_parameter_falls_back_to_the_default_window(self):
        response = self.client.get(self.metrics_url(), {"days": ""})

        self.assertEqual(response.data["window_days"], 30)

    def test_a_plain_member_cannot_read_the_metrics(self):
        member = self.make_user("metrics-plain-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.authenticated_client(member).get(self.metrics_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_unrelated_user_cannot_read_the_metrics(self):
        response = self.authenticated_client(self.make_user("metrics-outsider")).get(self.metrics_url())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_moderator_can_read_the_metrics(self):
        response = self.authenticated_client(self.moderator).get(self.metrics_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

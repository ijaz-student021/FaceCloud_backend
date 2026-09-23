"""T-007 Phase F: org-scoped reports (review-summary + moderator-activity only
-- see moderation/services.py's comment for why the other four originally
listed report types are deferred rather than attempted here).
"""
import json

from django.urls import reverse
from django.test import TestCase
from rest_framework import status

from organizations.models import OrganizationMembership
from organizations.services import create_organization

from .models import ModeratorReport
from .services import (
    build_moderator_activity_payload,
    build_review_summary_payload,
    enqueue_moderator_report,
    expire_due_moderator_reports,
)
from .tasks import process_moderator_report
from .tests import ModerationFixtureMixin


class ReportPayloadTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("report-owner")
        self.organization = create_organization(name="Report Co", owner=self.owner)
        self.review, self.decision = self.make_quality_review(self.owner, self.organization)

        self.other_owner = self.make_user("other-report-owner")
        self.other_org = create_organization(name="Other Report Co", owner=self.other_owner)
        self.make_quality_review(self.other_owner, self.other_org)

    def test_review_summary_includes_this_orgs_reviews_only(self):
        payload = build_review_summary_payload(self.organization)

        self.assertEqual(payload["summary"]["total_reviews"], 1)
        self.assertEqual(payload["rows"][0]["id"], self.review.pk)

    def test_moderator_activity_counts_decisions_per_moderator(self):
        from .services import decide_review
        decide_review(review=self.review, actor=self.owner, action="approve")

        payload = build_moderator_activity_payload(self.organization)

        moderators = payload["summary"]["moderators"]
        self.assertEqual(len(moderators), 1)
        self.assertEqual(moderators[0]["decided_by_id"], self.owner.pk)
        self.assertEqual(moderators[0]["approved"], 1)
        self.assertGreaterEqual(len(payload["rows"]), 1)

    def test_neither_report_leaks_another_organizations_data(self):
        summary = build_review_summary_payload(self.organization)
        activity = build_moderator_activity_payload(self.organization)

        self.assertEqual(summary["organization"]["id"], self.organization.pk)
        self.assertEqual(summary["summary"]["total_reviews"], 1)
        serialized = json.dumps(summary) + json.dumps(activity)
        self.assertNotIn(self.other_owner.username, serialized)
        self.assertNotIn(self.other_org.name, serialized)


class ReportEnqueueAndProcessingTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("process-report-owner")
        self.organization = create_organization(name="Process Report Co", owner=self.owner)
        self.make_quality_review(self.owner, self.organization)

    def test_enqueueing_creates_a_queued_report(self):
        report, created = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.JSON,
        )

        self.assertTrue(created)
        self.assertEqual(report.status, ModeratorReport.Status.QUEUED)

    def test_a_second_enqueue_of_the_same_type_and_format_reuses_it(self):
        first, _ = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.JSON,
        )

        second, created = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.JSON,
        )

        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)

    def test_a_different_format_is_a_separate_report(self):
        enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.JSON,
        )

        _, created = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.CSV,
        )

        self.assertTrue(created)

    def test_processing_produces_a_downloadable_json_file(self):
        report, _ = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.JSON,
        )

        processed = process_moderator_report(report.pk, report.task_id)

        self.assertEqual(processed.status, ModeratorReport.Status.READY)
        self.assertTrue(processed.report_file.name)
        self.assertGreater(processed.file_size, 0)
        self.assertEqual(processed.content_type, "application/json")

    def test_processing_produces_a_downloadable_pdf_file(self):
        report, _ = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.MODERATOR_ACTIVITY, report_format=ModeratorReport.Format.PDF,
        )

        processed = process_moderator_report(report.pk, report.task_id)

        self.assertEqual(processed.status, ModeratorReport.Status.READY)
        self.assertEqual(processed.content_type, "application/pdf")

    def test_processing_produces_a_downloadable_csv_file(self):
        report, _ = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.CSV,
        )

        processed = process_moderator_report(report.pk, report.task_id)

        self.assertEqual(processed.status, ModeratorReport.Status.READY)
        self.assertEqual(processed.content_type, "text/csv; charset=utf-8")


class ReportApiTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("report-api-owner")
        self.organization = create_organization(name="Report Api Co", owner=self.owner)
        self.make_quality_review(self.owner, self.organization)
        self.owner_client = self.authenticated_client(self.owner)
        self.list_url = reverse(
            "moderation:report-list-create", kwargs={"organization_id": self.organization.pk},
        )

    def test_a_plain_member_cannot_request_a_report(self):
        member = self.make_user("report-api-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.authenticated_client(member).post(
            self.list_url, {"report_type": "review_summary"}, format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_owner_can_request_a_report(self):
        response = self.owner_client.post(self.list_url, {"report_type": "review_summary"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["report_type"], "review_summary")

    def test_downloading_before_ready_is_a_conflict(self):
        created = self.owner_client.post(self.list_url, {"report_type": "review_summary"}, format="json").data

        response = self.owner_client.get(
            reverse(
                "moderation:report-download",
                kwargs={"organization_id": self.organization.pk, "report_id": created["id"]},
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_downloading_once_ready_returns_the_file(self):
        created = self.owner_client.post(self.list_url, {"report_type": "review_summary"}, format="json").data
        process_moderator_report(created["id"], ModeratorReport.objects.get(pk=created["id"]).task_id)

        response = self.owner_client.get(
            reverse(
                "moderation:report-download",
                kwargs={"organization_id": self.organization.pk, "report_id": created["id"]},
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ReportExpiryTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("report-expiry-owner")
        self.organization = create_organization(name="Report Expiry Co", owner=self.owner)
        self.make_quality_review(self.owner, self.organization)
        report, _ = enqueue_moderator_report(
            organization=self.organization, requested_by=self.owner,
            report_type=ModeratorReport.ReportType.REVIEW_SUMMARY, report_format=ModeratorReport.Format.JSON,
        )
        self.report = process_moderator_report(report.pk, report.task_id)

    def test_a_past_ttl_ready_report_is_marked_expired_and_its_file_removed(self):
        from django.utils import timezone
        ModeratorReport.objects.filter(pk=self.report.pk).update(expires_at=timezone.now() - timezone.timedelta(hours=1))

        expired_count = expire_due_moderator_reports()

        self.assertEqual(expired_count, 1)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, ModeratorReport.Status.EXPIRED)
        self.assertFalse(self.report.report_file)

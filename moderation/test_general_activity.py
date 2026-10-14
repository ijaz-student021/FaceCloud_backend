"""The general-activity organization report (previously missing)."""
import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from organizations.models import OrganizationAuditEvent
from organizations.services import create_organization, record_audit_event

from .models import ModeratorReport
from .renderers import render_moderator_report
from .services import build_moderator_report_payload
from .tasks import process_moderator_report
from .test_more_reports import FORBIDDEN_WORDS, ReportFixtureMixin

Action = OrganizationAuditEvent.Action


class GeneralActivityReportTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("activity-owner")
        self.organization = create_organization(name="Activity Co", owner=self.owner)
        self.other_owner = self.make_user("activity-other")
        self.other = create_organization(name="Elsewhere", owner=self.other_owner)
        record_audit_event(self.organization, self.owner, Action.MEMBER_INVITED, target_type="invitation", target_id=1)
        record_audit_event(self.organization, self.owner, Action.MEMBER_ROLE_CHANGED, target_type="membership",
                           target_id=2, reason="promoted")
        record_audit_event(self.organization, self.owner, Action.MEMBER_REMOVED)
        OrganizationAuditEvent.objects.filter(action=Action.MEMBER_REMOVED).update(
            created_at=timezone.now() - timedelta(days=60))
        record_audit_event(self.other, self.other_owner, Action.MEMBER_INVITED, reason="other org secret")
        self.make_batch(self.owner, self.organization, total_files=4)

    def payload(self, period=None):
        report = ModeratorReport(organization=self.organization, report_type="general_activity",
                                 date_from=period[0] if period else None, date_to=period[1] if period else None)
        return build_moderator_report_payload(report)

    def test_it_summarises_the_whole_audit_trail_and_period_totals(self):
        body = self.payload()
        summary = body["summary"]
        self.assertEqual(summary["audit_events"], OrganizationAuditEvent.objects.filter(
            organization=self.organization).count())
        self.assertEqual(summary["by_action"][Action.MEMBER_ROLE_CHANGED], 1)
        self.assertEqual(summary["by_actor"]["activity-owner"], summary["audit_events"])
        self.assertEqual((summary["batches_created"], summary["images_uploaded"]), (1, 4))
        self.assertIn("promoted", {row["reason"] for row in body["rows"]})

    def test_a_window_excludes_older_events(self):
        week = (timezone.now() - timedelta(days=7), timezone.now() + timedelta(minutes=1))
        actions = {row["action"] for row in self.payload(week)["rows"]}
        self.assertNotIn(Action.MEMBER_REMOVED, actions)
        self.assertIn(Action.MEMBER_REMOVED, {row["action"] for row in self.payload()["rows"]})

    def test_another_organization_never_appears(self):
        self.assertNotIn("other org secret", json.dumps(self.payload()))

    def test_it_carries_no_facial_data_and_renders_in_every_format(self):
        payload = self.payload()
        text = json.dumps(payload).lower()
        for word in FORBIDDEN_WORDS:
            self.assertNotIn(word, text)
        for report_format in ModeratorReport.Format.values:
            content, _type, _ext = render_moderator_report(payload, report_format)
            self.assertGreater(len(content), 0)

    def test_the_api_and_the_job_produce_it(self):
        client = self.authenticated_client(self.owner)
        url = reverse("moderation:report-list-create", kwargs={"organization_id": self.organization.pk})
        response = client.post(url, {"report_type": "general_activity", "format": "pdf"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        report = ModeratorReport.objects.get(pk=response.json()["id"])
        processed = process_moderator_report(report.pk, report.task_id)
        self.assertEqual(processed.status, ModeratorReport.Status.READY)

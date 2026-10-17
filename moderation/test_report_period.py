"""Reporting windows: stored, applied by every report type, and part of dedup (B05)."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase
from django.urls import reverse

from duplicates.models import DuplicateMatch
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import UploadBatch

from .models import ModeratorReport, ModeratorReview
from .renderers import render_moderator_report
from .services import build_moderator_report_payload, enqueue_moderator_report
from .test_more_reports import ReportFixtureMixin

INSIDE = datetime(2026, 9, 10, 12, 0, tzinfo=dt_timezone.utc)
OUTSIDE = datetime(2026, 8, 1, 12, 0, tzinfo=dt_timezone.utc)
WINDOW = (datetime(2026, 9, 1, tzinfo=dt_timezone.utc), datetime(2026, 10, 1, tzinfo=dt_timezone.utc))


class ReportPeriodTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("period-owner")
        self.organization = create_organization(name="Period Co", owner=self.owner)
        self.client = self.authenticated_client(self.owner)
        self.url = reverse("moderation:report-list-create", kwargs={"organization_id": self.organization.pk})
        for moment in (INSIDE, OUTSIDE):
            self.one_of_each(moment)

    STAMPED = ((ModeratorReview, ("created_at", "decided_at")), (ImageQualityDecision, ("updated_at",)),
               (UploadBatch, ("created_at",)), (DuplicateMatch, ("created_at",)))

    def one_of_each(self, moment):
        """One record of every kind, all of them (helpers' extras included) at ``moment``."""
        before = {model: (model.objects.order_by("-pk").values_list("pk", flat=True).first() or 0)
                  for model, _fields in self.STAMPED}
        self.make_quality_review(self.owner, self.organization)
        self.make_decision(self.owner, self.organization, "rejected", ["too_dark"], 30.0)
        self.make_batch(self.owner, self.organization)
        self.make_duplicate_review(self.owner, self.organization)
        for model, fields in self.STAMPED:
            model.objects.filter(pk__gt=before[model]).update(**{field: moment for field in fields})
        ModeratorReview.objects.filter(pk__gt=before[ModeratorReview]).update(
            decided_by=self.owner, status=ModeratorReview.Status.APPROVED)

    def payload(self, report_type, period=WINDOW):
        report = ModeratorReport(organization=self.organization, report_type=report_type,
                                 date_from=period[0] if period else None, date_to=period[1] if period else None)
        return build_moderator_report_payload(report)

    def test_every_report_type_counts_only_records_inside_the_window(self):
        counted = {
            "review_summary": lambda body: body["summary"]["total_reviews"],
            "moderator_activity": lambda body: sum(row["total"] for row in body["summary"]["moderators"]),
            "rejection_reasons": lambda body: body["summary"]["flagged_photos"],
            "quality_scores": lambda body: body["summary"]["scored_photos"],
            "batches": lambda body: body["summary"]["total_batches"],
            "duplicate_decisions": lambda body: body["summary"]["total_matches"],
            "general_activity": lambda body: body["summary"]["reviews_created"],
        }
        self.assertEqual(set(counted), set(ModeratorReport.ReportType.values))
        for report_type, count in counted.items():
            with self.subTest(report_type=report_type):
                windowed, everything = self.payload(report_type), self.payload(report_type, None)
                # The same records were made inside and outside the window.
                self.assertGreater(count(windowed), 0)
                self.assertEqual(count(windowed) * 2, count(everything))
                self.assertEqual(windowed["period"]["from"], WINDOW[0].isoformat())
                self.assertEqual(windowed["period"]["timezone"], "UTC")
                self.assertTrue(windowed["period"]["filtered_on"])
                self.assertIsNone(everything["period"]["from"])

    def test_the_window_is_half_open(self):
        batch = self.make_batch(self.owner, self.organization)
        UploadBatch.objects.filter(pk=batch.pk).update(created_at=WINDOW[1])  # exactly at the end: excluded
        inside = self.payload("batches")["summary"]["total_batches"]
        start = self.make_batch(self.owner, self.organization)
        UploadBatch.objects.filter(pk=start.pk).update(created_at=WINDOW[0])  # exactly at the start: included
        self.assertEqual(self.payload("batches")["summary"]["total_batches"], inside + 1)

    def test_files_in_every_format_reflect_the_window(self):
        payload = self.payload("batches")
        inside = payload["summary"]["total_batches"]
        content, _type, _ext = render_moderator_report(payload, "json")
        self.assertEqual(json.loads(content)["summary"]["total_batches"], inside)
        content, _type, _ext = render_moderator_report(payload, "csv")
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), inside)
        self.assertTrue(all(row["created_at"].startswith("2026-09") for row in rows))
        content, _type, _ext = render_moderator_report(payload, "pdf")
        self.assertTrue(content.startswith(b"%PDF"))

    def test_the_api_stores_the_window_and_a_date_includes_its_whole_day(self):
        response = self.client.post(self.url, {"report_type": "batches", "format": "csv",
                                               "date_from": "2026-09-01", "date_to": "2026-09-30"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["date_from"], "2026-09-01T00:00:00Z")
        self.assertEqual(response.json()["date_to"], "2026-10-01T00:00:00Z")

    def test_different_windows_do_not_share_an_in_flight_report_but_the_same_one_does(self):
        common = {"organization": self.organization, "requested_by": self.owner,
                  "report_type": "batches", "report_format": "json"}
        september, created = enqueue_moderator_report(**common, date_from=WINDOW[0], date_to=WINDOW[1])
        self.assertTrue(created)
        again, created = enqueue_moderator_report(**common, date_from=WINDOW[0], date_to=WINDOW[1])
        self.assertEqual((again.pk, created), (september.pk, False))
        august, created = enqueue_moderator_report(
            **common, date_from=WINDOW[0] - timedelta(days=31), date_to=WINDOW[0])
        self.assertTrue(created)
        unbounded, created = enqueue_moderator_report(**common)
        self.assertTrue(created)
        self.assertEqual(len({september.pk, august.pk, unbounded.pk}), 3)

    def test_invalid_windows_are_refused(self):
        for body in (
            {"date_from": "2026-09-10", "date_to": "2026-09-01"},
            {"date_from": "yesterday"},
            {"date_from": "2025-01-01", "date_to": "2026-06-01"},
        ):
            with self.subTest(body=body):
                response = self.client.post(self.url, {"report_type": "batches", **body}, format="json")
                self.assertEqual(response.status_code, 400, response.content)

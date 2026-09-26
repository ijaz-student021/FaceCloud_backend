"""The four extra moderator report types: rejection reasons, quality scores, batches, duplicate decisions.

Each is scoped to one organization, contains no facial data (no embeddings, identity
groups, usernames of other organizations or face-similarity values), and renders in
JSON, CSV and PDF through the same asynchronous job as the original two.
"""
import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from duplicates.models import DuplicateMatch
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, UploadBatch

from . import services
from .models import ModeratorReport
from .renderers import render_moderator_report
from .services import (
    build_batches_payload,
    build_duplicate_decisions_payload,
    build_moderator_report_payload,
    build_quality_scores_payload,
    build_rejection_reasons_payload,
    enqueue_moderator_report,
)
from .tasks import process_moderator_report
from .tests import ModerationFixtureMixin

NEW_TYPES = [
    ModeratorReport.ReportType.REJECTION_REASONS,
    ModeratorReport.ReportType.QUALITY_SCORES,
    ModeratorReport.ReportType.BATCHES,
    ModeratorReport.ReportType.DUPLICATE_DECISIONS,
]
FORBIDDEN_WORDS = ("embedding", "centroid", "identity", "face_similarity", "landmark")


class ReportFixtureMixin(ModerationFixtureMixin):
    def make_decision(self, owner, organization, decision, reasons=(), overall=None, **scores):
        image = self.make_image(owner, organization)
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        return ImageQualityDecision.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, status="completed",
            decision=decision, overall_score=overall, rejection_reasons=list(reasons), **scores,
        )

    def make_batch(self, owner, organization, **fields):
        defaults = {"upload_type": "multiple", "total_files": 3, "accepted_files": 3, "processing_status": "completed"}
        defaults.update(fields)
        return UploadBatch.objects.create(owner=owner, organization=organization, **defaults)


class RejectionReasonsTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("reasons-owner")
        self.organization = create_organization(name="Reasons Co", owner=self.owner)
        self.other_owner = self.make_user("reasons-other")
        self.other_org = create_organization(name="Reasons Elsewhere", owner=self.other_owner)

    def test_reasons_are_counted_most_common_first_and_only_flagged_photos_appear(self):
        self.make_decision(self.owner, self.organization, "rejected", ["too_dark", "too_blurry"], 20.0)
        self.make_decision(self.owner, self.organization, "needs_review", ["too_dark"], 55.0)
        self.make_decision(self.owner, self.organization, "accepted", [], 90.0)

        payload = build_rejection_reasons_payload(self.organization)

        self.assertEqual(payload["summary"]["flagged_photos"], 2)
        self.assertEqual(payload["summary"]["reasons"], {"too_dark": 2, "too_blurry": 1})
        self.assertEqual(list(payload["summary"]["reasons"])[0], "too_dark")
        self.assertEqual(payload["summary"]["by_decision"], {"rejected": 1, "needs_review": 1})
        self.assertEqual(payload["row_count"], 2)
        self.assertIn("too_dark, too_blurry", {row["reasons"] for row in payload["rows"]})

    def test_unfinished_decisions_and_malformed_reasons_are_ignored(self):
        pending = self.make_decision(self.owner, self.organization, "rejected", ["too_dark"], 10.0)
        ImageQualityDecision.objects.filter(pk=pending.pk).update(status="processing")
        self.make_decision(self.owner, self.organization, "rejected", [None, 5, {"x": 1}, "noisy"], 15.0)
        payload = build_rejection_reasons_payload(self.organization)
        self.assertEqual(payload["summary"]["reasons"], {"noisy": 1})

    def test_another_organizations_photos_never_appear(self):
        self.make_decision(self.other_owner, self.other_org, "rejected", ["secret_reason"], 5.0)
        payload = build_rejection_reasons_payload(self.organization)
        self.assertEqual(payload["summary"]["flagged_photos"], 0)
        self.assertNotIn("secret_reason", json.dumps(payload))


class QualityScoresTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("scores-owner")
        self.organization = create_organization(name="Scores Co", owner=self.owner)

    def test_averages_and_distribution(self):
        for overall in (10.0, 30.0, 70.0, 90.0, 100.0):
            self.make_decision(self.owner, self.organization, "accepted", (), overall, brightness_score=overall)
        self.make_decision(self.owner, self.organization, "accepted", (), None)  # unscored: excluded

        payload = build_quality_scores_payload(self.organization)

        self.assertEqual(payload["summary"]["scored_photos"], 5)
        self.assertEqual(payload["summary"]["mean_overall_score"], 60.0)
        self.assertEqual(payload["summary"]["mean_subscores"]["brightness_score"], 60.0)
        self.assertIsNone(payload["summary"]["mean_subscores"]["noise_score"])
        distribution = payload["summary"]["score_distribution"]
        self.assertEqual(distribution, {"0-20": 1, "20-40": 1, "40-60": 0, "60-80": 1, "80-100": 2})
        self.assertEqual(sum(distribution.values()), payload["summary"]["scored_photos"])

    def test_an_empty_organization_gives_a_valid_empty_report(self):
        payload = build_quality_scores_payload(self.organization)
        self.assertEqual(payload["summary"]["scored_photos"], 0)
        self.assertIsNone(payload["summary"]["mean_overall_score"])
        self.assertEqual(payload["rows"], [])

    def test_rows_are_capped_but_the_summary_counts_everything(self):
        for _ in range(3):
            self.make_decision(self.owner, self.organization, "accepted", (), 50.0)
        with patch.object(services, "REPORT_ROW_LIMIT", 2):
            payload = build_quality_scores_payload(self.organization)
        self.assertEqual(payload["summary"]["scored_photos"], 3)
        self.assertEqual(payload["row_count"], 2)


class BatchesReportTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("batches-owner")
        self.organization = create_organization(name="Batches Co", owner=self.owner)
        self.other_owner = self.make_user("batches-other")
        self.other_org = create_organization(name="Batches Elsewhere", owner=self.other_owner)

    def test_totals_status_counts_and_rows(self):
        self.make_batch(self.owner, self.organization, total_files=10, accepted_files=8, rejected_files=2)
        self.make_batch(
            self.owner, self.organization, total_files=4, accepted_files=4, processing_status="failed",
            processing_failed_files=4, retry_count=2,
        )
        self.make_batch(self.other_owner, self.other_org, total_files=99, accepted_files=99)

        payload = build_batches_payload(self.organization)

        self.assertEqual(payload["summary"]["total_batches"], 2)
        self.assertEqual(payload["summary"]["by_processing_status"], {"completed": 1, "failed": 1})
        self.assertEqual(payload["summary"]["total_files"], 14)
        self.assertEqual(payload["summary"]["accepted_files"], 12)
        self.assertEqual(payload["summary"]["rejected_files"], 2)
        self.assertEqual(payload["summary"]["processing_failed_files"], 4)
        self.assertEqual(payload["summary"]["retries"], 2)
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["rows"][0]["processing_status"], "failed")  # newest first

    def test_an_organization_with_no_batches_reports_zeros(self):
        payload = build_batches_payload(self.organization)
        self.assertEqual(payload["summary"]["total_batches"], 0)
        self.assertEqual(payload["summary"]["total_files"], 0)


class DuplicateDecisionsTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("dupes-owner")
        self.organization = create_organization(name="Dupes Co", owner=self.owner)
        self.other_owner = self.make_user("dupes-other")
        self.other_org = create_organization(name="Dupes Elsewhere", owner=self.other_owner)

    def test_matches_are_counted_by_decision_and_type_and_scoped_to_the_organization(self):
        review, _ = self.make_duplicate_review(self.owner, self.organization)
        confirmed, _ = self.make_duplicate_review(self.owner, self.organization)
        DuplicateMatch.objects.filter(pk=confirmed.duplicate_match_id).update(review_status="confirmed")
        self.make_duplicate_review(self.other_owner, self.other_org)

        payload = build_duplicate_decisions_payload(self.organization)

        self.assertEqual(payload["summary"]["total_matches"], 2)
        self.assertEqual(payload["summary"]["by_review_status"], {"suspected": 1, "confirmed": 1})
        self.assertEqual(payload["summary"]["by_match_type"], {"perceptual": 2})
        self.assertEqual(payload["summary"]["awaiting_decision"], 1)
        self.assertEqual({row["match_id"] for row in payload["rows"]}, {review.duplicate_match_id, confirmed.duplicate_match_id})

    def test_no_face_similarity_or_owner_identity_is_exposed(self):
        self.make_duplicate_review(self.owner, self.organization)
        payload = build_duplicate_decisions_payload(self.organization)
        self.assertEqual(
            set(payload["rows"][0]),
            {"match_id", "source_image_id", "matched_image_id", "match_type", "similarity_score", "review_status", "created_at"},
        )


class NewReportTypesEndToEndTests(ReportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("e2e-report-owner")
        self.organization = create_organization(name="E2E Reports Co", owner=self.owner)
        self.make_decision(self.owner, self.organization, "rejected", ["too_dark"], 12.5, sharpness_score=30.0)
        self.make_batch(self.owner, self.organization)
        self.make_duplicate_review(self.owner, self.organization)
        self.client = self.authenticated_client(self.owner)
        self.list_url = reverse("moderation:report-list-create", kwargs={"organization_id": self.organization.pk})

    def test_no_payload_carries_facial_data(self):
        for report_type in NEW_TYPES:
            report = ModeratorReport(organization=self.organization, report_type=report_type)
            text = json.dumps(build_moderator_report_payload(report)).lower()
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, text, f"{report_type} mentions {word}")

    def test_every_type_renders_in_every_format_with_data_and_when_empty(self):
        empty_owner = self.make_user("empty-report-owner")
        empty_org = create_organization(name="Empty Reports Co", owner=empty_owner)
        for organization in (self.organization, empty_org):
            for report_type in NEW_TYPES:
                report = ModeratorReport(organization=organization, report_type=report_type)
                payload = build_moderator_report_payload(report)
                for report_format in ModeratorReport.Format.values:
                    content, content_type, _ = render_moderator_report(payload, report_format)
                    self.assertGreater(len(content), 0, f"{report_type}/{report_format}")
                    if report_format == "pdf":
                        self.assertTrue(content.startswith(b"%PDF"))
                    if report_format == "json":
                        self.assertEqual(json.loads(content)["report_type"], report_type)

    def test_the_async_job_produces_a_ready_file_for_each_new_type(self):
        for report_type in NEW_TYPES:
            report, created = enqueue_moderator_report(
                organization=self.organization, requested_by=self.owner,
                report_type=report_type, report_format=ModeratorReport.Format.CSV,
            )
            self.assertTrue(created)
            processed = process_moderator_report(report.pk, report.task_id)
            self.assertEqual(processed.status, ModeratorReport.Status.READY, report_type)
            self.assertGreater(processed.file_size, 0)

    def test_the_api_accepts_each_new_type_and_rejects_an_unknown_one(self):
        for report_type in NEW_TYPES:
            response = self.client.post(self.list_url, {"report_type": report_type, "format": "json"}, format="json")
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, (report_type, response.data))
            self.assertEqual(response.data["report_type"], report_type)
        bad = self.client.post(self.list_url, {"report_type": "everything"}, format="json")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

import csv
import io
import json
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from duplicates.models import DuplicateAnalysis
from enhancement.models import ImageEnhancementResult
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ProcessingReport
from .services import build_report_payload
from .tasks import process_processing_report


class ProcessingReportTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-reports-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.user = User.objects.create_user(
            username="report-owner",
            email="report-owner@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.other_user = User.objects.create_user(
            username="other-report-owner",
            email="other-report-owner@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.client.force_authenticate(self.user)
        self.batch = UploadBatch.objects.create(
            owner=self.user,
            upload_type="single",
            display_name="Portrait batch",
            status="uploaded",
            total_files=1,
            accepted_files=1,
            total_size=1200,
            processing_status="completed",
            processed_files=1,
            current_stage="completed",
        )
        self.image = self._create_completed_image(self.batch, "portrait.jpg")

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _create_completed_image(self, batch, name, *, status_value="uploaded"):
        image = ImageUpload.objects.create(
            owner=batch.owner,
            batch=batch,
            original_name=name,
            file_extension=".jpg",
            mime_type="image/jpeg",
            file_size=1200,
            width=800,
            height=1000,
            checksum_sha256="a" * 64,
            status=status_value,
            rejection_reason="" if status_value == "uploaded" else "Unsupported image",
        )
        image.original_file.save(name, ContentFile(b"original-image"), save=True)
        if status_value == "rejected":
            return image
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=batch.owner,
            image=image,
            status="completed",
            requested_format="auto",
            original_format="jpeg",
            output_format="jpeg",
            output_name=f"standardized-{name}",
            original_width=800,
            original_height=1000,
            output_width=800,
            output_height=1000,
            original_file_size=1200,
            processed_file_size=1100,
            processing_duration_ms=20,
        )
        preprocessing.processed_file.save(
            f"standardized-{name}", ContentFile(b"standardized-image"), save=True
        )
        face = FaceValidationResult.objects.create(
            owner=batch.owner,
            image=image,
            preprocessing_result=preprocessing,
            decision="accepted",
            face_count=3,
            primary_confidence=0.94,
            detector="opencv-test",
            processing_duration_ms=12,
        )
        ImageQualityDecision.objects.create(
            owner=batch.owner,
            image=image,
            preprocessing_result=preprocessing,
            face_validation=face,
            status="completed",
            decision="accepted",
            overall_score=82.25,
            brightness_score=79.5,
            sharpness_score=71.5,
            contrast_score=80,
            noise_score=78,
            exposure_score=84,
            resolution_score=91,
            policy_version="quality_policy_test",
            processing_duration_ms=18,
        )
        result = ImageEnhancementResult.objects.create(
            owner=batch.owner,
            image=image,
            preprocessing_result=preprocessing,
            requested_level="auto",
            applied_level="medium",
            status="completed",
            output_name=f"enhanced-{name}",
            output_format="jpeg",
            output_width=800,
            output_height=1000,
            output_file_size=1150,
            processing_duration_ms=30,
            applied_parameters={
                "algorithm": "facecloud_curve_v1",
                "private_internal_setting": "must-not-leak",
            },
        )
        result.enhanced_file.save(
            f"enhanced-{name}", ContentFile(b"enhanced-image"), save=True
        )
        DuplicateAnalysis.objects.create(
            owner=batch.owner,
            image=image,
            preprocessing_result=preprocessing,
            status="completed",
            decision="no_duplicate",
            perceptual_hash="private-hash",
            face_fingerprints=["private-fingerprint"],
            match_count=0,
            policy_version="duplicate_policy_test",
            processing_duration_ms=5,
        )
        return image

    def _report(self, report_format="json", *, image=None, batch=None):
        return ProcessingReport.objects.create(
            owner=self.user,
            report_type="image" if image else "batch",
            format=report_format,
            image=image,
            batch=batch,
            status="queued",
            task_id="direct-test-task",
        )

    def test_payload_is_normalized_complete_and_does_not_leak_private_evidence(self):
        report = self._report(image=self.image)
        payload = build_report_payload(report)
        record = payload["images"][0]
        self.assertEqual(payload["schema_version"], "processing_report_v1")
        self.assertEqual(record["quality"]["blur_score"], 28.5)
        self.assertEqual(record["face_validation"]["face_count"], 3)
        self.assertEqual(record["face_validation"]["primary_confidence"], 94.0)
        self.assertEqual(record["identity_group"], {
            "available": False, "group_id": None, "confidence": None,
        })
        self.assertEqual(record["processing_duration_ms"], 85)
        self.assertTrue(record["protected_links"]["enhanced"].startswith("/api/"))
        serialized = json.dumps(payload)
        self.assertNotIn("private-hash", serialized)
        self.assertNotIn("private-fingerprint", serialized)
        self.assertNotIn("private_internal_setting", serialized)

    def test_json_report_generation_stores_immutable_snapshot(self):
        report = process_processing_report(
            self._report("json", image=self.image).pk,
            "direct-test-task",
        )
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.content_type, "application/json")
        self.assertEqual(len(report.file_checksum_sha256), 64)
        with report.report_file.open("rb") as report_file:
            document = json.loads(report_file.read().decode("utf-8"))
        self.assertEqual(document, report.payload)
        self.assertEqual(document["images"][0]["filename"], "portrait.jpg")

    def test_csv_report_has_bom_and_blocks_spreadsheet_formula_injection(self):
        self.image.original_name = "=DANGEROUS().jpg"
        self.image.save(update_fields=["original_name", "updated_at"])
        report = process_processing_report(
            self._report("csv", image=self.image).pk,
            "direct-test-task",
        )
        with report.report_file.open("rb") as report_file:
            content = report_file.read().decode("utf-8-sig")
        row = next(csv.DictReader(io.StringIO(content)))
        self.assertEqual(row["filename"], "'=DANGEROUS().jpg")
        self.assertEqual(row["blur_score"], "28.5")

    def test_pdf_report_is_valid_pdf_document(self):
        report = process_processing_report(
            self._report("pdf", image=self.image).pk,
            "direct-test-task",
        )
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.content_type, "application/pdf")
        with report.report_file.open("rb") as report_file:
            self.assertTrue(report_file.read(5).startswith(b"%PDF"))

    def test_batch_report_contains_all_images_and_status_summary(self):
        rejected = self._create_completed_image(
            self.batch, "rejected.png", status_value="rejected"
        )
        self.batch.total_files = 2
        self.batch.accepted_files = 1
        self.batch.rejected_files = 1
        self.batch.save(update_fields=[
            "total_files", "accepted_files", "rejected_files", "updated_at",
        ])
        report = process_processing_report(
            self._report("json", batch=self.batch).pk,
            "direct-test-task",
        )
        self.assertEqual(report.payload["summary"]["total_images"], 2)
        statuses = {item["image_id"]: item["status"] for item in report.payload["images"]}
        self.assertEqual(statuses[rejected.pk], "rejected")
        self.assertEqual(report.payload["summary"]["status_counts"]["accepted"], 1)

    def test_create_endpoint_queues_report_and_reuses_active_request(self):
        url = reverse("reports:image-create", args=[self.image.pk])
        with patch("reports.tasks.generate_processing_report_task.apply_async") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, {"format": "pdf"}, format="json")
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertTrue(response.data["created"])
            queued.assert_called_once()
            duplicate = self.client.post(url, {"format": "pdf"}, format="json")
        self.assertEqual(duplicate.status_code, status.HTTP_200_OK)
        self.assertFalse(duplicate.data["created"])
        self.assertEqual(
            response.data["report"]["id"], duplicate.data["report"]["id"]
        )

    def test_not_ready_image_returns_conflict(self):
        batch = UploadBatch.objects.create(
            owner=self.user,
            upload_type="single",
            status="uploaded",
            total_files=1,
            accepted_files=1,
            processing_status="processing",
        )
        image = ImageUpload.objects.create(
            owner=self.user,
            batch=batch,
            original_name="pending.jpg",
            status="uploaded",
        )
        response = self.client.post(
            reverse("reports:image-create", args=[image.pk]),
            {"format": "json"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["detail"].code, "report_not_ready")

    def test_owner_isolation_hides_sources_reports_and_downloads(self):
        ready = process_processing_report(
            self._report("json", image=self.image).pk,
            "direct-test-task",
        )
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get(reverse("reports:list")).data["count"], 0)
        self.assertEqual(
            self.client.get(reverse("reports:detail", args=[ready.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(reverse("reports:download", args=[ready.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                reverse("reports:image-create", args=[self.image.pk]),
                {"format": "json"}, format="json",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_ready_report_download_and_unready_download_conflict(self):
        queued = self._report("json", image=self.image)
        conflict = self.client.get(reverse("reports:download", args=[queued.pk]))
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        ready = process_processing_report(queued.pk, "direct-test-task")
        response = self.client.get(reverse("reports:download", args=[ready.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content)[:1], b"{")

    def test_queue_failure_is_recorded_without_exposing_exception(self):
        url = reverse("reports:image-create", args=[self.image.pk])
        with patch(
            "reports.tasks.generate_processing_report_task.apply_async",
            side_effect=ConnectionError("private redis details"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, {"format": "csv"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        report = ProcessingReport.objects.get(pk=response.data["report"]["id"])
        self.assertEqual(report.status, "failed")
        self.assertEqual(
            report.failure_reason, "The report-generation queue is unavailable."
        )
        self.assertNotIn("redis", report.failure_reason.lower())

    def test_list_filters_and_regeneration_use_requested_format(self):
        original = process_processing_report(
            self._report("json", image=self.image).pk,
            "direct-test-task",
        )
        response = self.client.get(reverse("reports:list"), {"status": "ready"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        with patch("reports.tasks.generate_processing_report_task.apply_async"):
            with self.captureOnCommitCallbacks(execute=True):
                regenerated = self.client.post(
                    reverse("reports:regenerate", args=[original.pk]),
                    {"format": "pdf"},
                    format="json",
                )
        self.assertEqual(regenerated.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(regenerated.data["report"]["format"], "pdf")

    def test_deleting_report_removes_its_stored_file(self):
        report = process_processing_report(
            self._report("json", image=self.image).pk,
            "direct-test-task",
        )
        storage = report.report_file.storage
        stored_name = report.report_file.name
        self.assertTrue(storage.exists(stored_name))
        report.delete()
        self.assertFalse(storage.exists(stored_name))

import io
import json
import shutil
import tempfile
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from enhancement.models import ImageEnhancementResult
from quality_control.models import ImageQualityDecision
from reports.models import ProcessingReport
from reports.tasks import process_processing_report
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import DownloadEvent, DownloadPackage
from .tasks import process_download_package


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about photographs the checks refused; the product default no longer refuses (quality_control/test_never_reject.py)
class SecureDownloadTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-downloads-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.user = User.objects.create_user(
            username="download-owner",
            email="download-owner@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="other-download-owner",
            email="other-download-owner@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.client.force_authenticate(self.user)
        self.batch = UploadBatch.objects.create(
            owner=self.user,
            upload_type="multiple",
            display_name="Client / Portraits",
            status="uploaded",
            total_files=2,
            accepted_files=2,
            total_size=2400,
            processing_status="completed",
            processed_files=2,
            current_stage="completed",
        )
        self.image, self.preprocessing, self.enhancement = self._completed_image(
            self.batch, "same portrait.jpg", decision="accepted"
        )
        self.second_image, _, self.second_enhancement = self._completed_image(
            self.batch, "same portrait.jpg", decision="rejected"
        )

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _completed_image(self, batch, name, *, decision):
        image = ImageUpload.objects.create(
            owner=batch.owner,
            batch=batch,
            original_name=name,
            file_extension=".jpg",
            mime_type="image/jpeg",
            file_size=14,
            width=800,
            height=1000,
            checksum_sha256=("a" if decision == "accepted" else "b") * 64,
            status="uploaded",
        )
        image.original_file.save(name, ContentFile(b"original-image"), save=True)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=batch.owner,
            image=image,
            status="completed",
            output_format="jpeg",
            output_name=name,
            output_width=800,
            output_height=1000,
            processed_file_size=18,
            processing_duration_ms=10,
        )
        preprocessing.processed_file.save(
            name, ContentFile(b"standardized-image"), save=True
        )
        ImageQualityDecision.objects.create(
            owner=batch.owner,
            image=image,
            preprocessing_result=preprocessing,
            status="completed",
            decision=decision,
            overall_score=80 if decision == "accepted" else 35,
            brightness_score=75,
            sharpness_score=70,
            rejection_reasons=[] if decision == "accepted" else ["Image too blurry"],
            processing_duration_ms=10,
        )
        enhancement = ImageEnhancementResult.objects.create(
            owner=batch.owner,
            image=image,
            preprocessing_result=preprocessing,
            requested_level="auto",
            applied_level="medium",
            status="completed",
            output_name=name,
            output_format="jpeg",
            output_width=800,
            output_height=1000,
            output_file_size=14,
            processing_duration_ms=20,
            applied_parameters={"algorithm": "test-enhancer"},
        )
        enhancement.enhanced_file.save(
            name, ContentFile(b"enhanced-image"), save=True
        )
        return image, preprocessing, enhancement

    def _package(self, package_type="complete_batch", report_format="json"):
        return DownloadPackage.objects.create(
            owner=self.user,
            batch=self.batch,
            package_type=package_type,
            report_format=report_format,
            status="queued",
            task_id="direct-package-task",
        )

    @staticmethod
    def _zip_document(package):
        with package.package_file.open("rb") as package_file:
            payload = package_file.read()
        return zipfile.ZipFile(io.BytesIO(payload))

    def test_accepted_results_contains_only_accepted_enhanced_image_and_report(self):
        package = process_download_package(
            self._package("accepted_results", "csv").pk,
            "direct-package-task",
        )
        self.assertEqual(package.status, "ready")
        self.assertEqual(len(package.file_checksum_sha256), 64)
        with self._zip_document(package) as archive:
            names = archive.namelist()
            self.assertEqual(len([name for name in names if name.startswith("enhanced/")]), 1)
            self.assertIn("reports/batch-report.csv", names)
            self.assertIn("manifest.json", names)
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["package_type"], "accepted_results")
        self.assertEqual(manifest["summary"]["included_images"], 1)

    def test_complete_batch_has_safe_folders_unique_names_report_and_manifest(self):
        package = process_download_package(
            self._package("complete_batch", "json").pk,
            "direct-package-task",
        )
        with self._zip_document(package) as archive:
            names = archive.namelist()
            self.assertEqual(len([name for name in names if name.startswith("original/")]), 2)
            self.assertEqual(len([name for name in names if name.startswith("standardized/")]), 2)
            self.assertEqual(len([name for name in names if name.startswith("enhanced/")]), 2)
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("original/same-portrait.jpg", names)
            self.assertIn("original/same-portrait-2.jpg", names)
            self.assertIn("reports/batch-report.json", names)
            self.assertTrue(all(".." not in name and not name.startswith("/") for name in names))
            report = json.loads(archive.read("reports/batch-report.json"))
        self.assertEqual(report["summary"]["total_images"], 2)
        self.assertEqual(package.manifest["summary"]["included_files"], 6)

    def test_create_endpoint_queues_and_reuses_active_package(self):
        url = reverse("downloads:package-create", args=[self.batch.pk])
        request_body = {"package_type": "complete_batch", "report_format": "pdf"}
        with patch("downloads.tasks.generate_download_package_task.apply_async") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, request_body, format="json")
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertTrue(response.data["created"])
            queued.assert_called_once()
            duplicate = self.client.post(url, request_body, format="json")
        self.assertEqual(duplicate.status_code, status.HTTP_200_OK)
        self.assertFalse(duplicate.data["created"])
        self.assertEqual(
            response.data["package"]["id"], duplicate.data["package"]["id"]
        )

    def test_non_terminal_batch_and_missing_accepted_results_return_conflict(self):
        self.batch.processing_status = "processing"
        self.batch.save(update_fields=["processing_status", "updated_at"])
        response = self.client.post(
            reverse("downloads:package-create", args=[self.batch.pk]),
            {"package_type": "complete_batch"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.batch.processing_status = "completed"
        self.batch.save(update_fields=["processing_status", "updated_at"])
        ImageQualityDecision.objects.filter(owner=self.user).update(decision="rejected")
        response = self.client.post(
            reverse("downloads:package-create", args=[self.batch.pk]),
            {"package_type": "accepted_results"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_owner_isolation_hides_batch_packages_files_and_history(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        self.client.force_authenticate(self.other)
        self.assertEqual(
            self.client.post(
                reverse("downloads:package-create", args=[self.batch.pk]),
                {"package_type": "complete_batch"}, format="json",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(
                reverse("downloads:package-detail", args=[package.pk])
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(
                reverse("downloads:package-download", args=[package.pk])
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.client.get(reverse("downloads:history")).data["count"], 0)

    def test_package_download_records_owner_history(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        response = self.client.get(
            reverse("downloads:package-download", args=[package.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(b"".join(response.streaming_content).startswith(b"PK"))
        event = DownloadEvent.objects.get(resource_type="batch_package")
        self.assertEqual(event.owner, self.user)
        self.assertEqual(event.resource_id, package.pk)
        self.assertEqual(event.batch_id, self.batch.pk)

    def test_existing_download_endpoints_record_all_resource_types(self):
        report = ProcessingReport.objects.create(
            owner=self.user,
            report_type="image",
            format="json",
            image=self.image,
            status="queued",
            task_id="report-download-test",
        )
        report = process_processing_report(report.pk, "report-download-test")
        urls = (
            reverse("image-upload-download", args=[self.image.pk]),
            reverse("image-preprocessing-download", args=[self.image.pk]),
            reverse("enhancement:result-download", args=[self.enhancement.pk]),
            reverse("reports:download", args=[report.pk]),
        )
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            b"".join(response.streaming_content)
        self.assertEqual(
            set(DownloadEvent.objects.values_list("resource_type", flat=True)),
            {
                "original_image", "standardized_image", "enhanced_image",
                "processing_report",
            },
        )

    def test_expired_package_file_is_removed_and_can_be_regenerated(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        stored_name = package.package_file.name
        storage = package.package_file.storage
        package.expires_at = timezone.now() - timedelta(seconds=1)
        package.save(update_fields=["expires_at", "updated_at"])
        response = self.client.get(
            reverse("downloads:package-download", args=[package.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        package.refresh_from_db()
        self.assertEqual(package.status, "expired")
        self.assertFalse(storage.exists(stored_name))
        with patch("downloads.tasks.generate_download_package_task.apply_async"):
            with self.captureOnCommitCallbacks(execute=True):
                regenerated = self.client.post(
                    reverse("downloads:package-regenerate", args=[package.pk]),
                    {},
                    format="json",
                )
        self.assertEqual(regenerated.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotEqual(regenerated.data["package"]["id"], package.pk)

    def test_cleanup_command_expires_due_package(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        package.expires_at = timezone.now() - timedelta(seconds=1)
        package.save(update_fields=["expires_at", "updated_at"])
        call_command("cleanup_expired_downloads", verbosity=0)
        package.refresh_from_db()
        self.assertEqual(package.status, "expired")

    def test_queue_failure_records_generic_message(self):
        url = reverse("downloads:package-create", args=[self.batch.pk])
        with patch(
            "downloads.tasks.generate_download_package_task.apply_async",
            side_effect=ConnectionError("private redis detail"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    url,
                    {"package_type": "complete_batch", "report_format": "csv"},
                    format="json",
                )
        package = DownloadPackage.objects.get(pk=response.data["package"]["id"])
        self.assertEqual(package.status, "failed")
        self.assertEqual(
            package.failure_reason, "The download-package queue is unavailable."
        )
        self.assertNotIn("redis", package.failure_reason.lower())

    @override_settings(DOWNLOAD_PACKAGE_MAX_UNCOMPRESSED_SIZE=1)
    def test_package_size_limit_fails_safely_without_partial_file(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        self.assertEqual(package.status, "failed")
        self.assertFalse(package.package_file)
        self.assertEqual(
            package.failure_reason,
            "The download package could not be generated safely.",
        )

    def test_deleting_package_removes_physical_zip(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        storage = package.package_file.storage
        stored_name = package.package_file.name
        self.assertTrue(storage.exists(stored_name))
        package.delete()
        self.assertFalse(storage.exists(stored_name))

    def test_package_and_history_filters_are_validated(self):
        package = process_download_package(
            self._package().pk, "direct-package-task"
        )
        response = self.client.get(
            reverse("downloads:package-list"),
            {"status": "ready", "package_type": "complete_batch"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], package.pk)
        invalid = self.client.get(
            reverse("downloads:package-list"), {"status": "unknown"}
        )
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

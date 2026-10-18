from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from downloads.models import DownloadPackage
from enhancement.models import (
    EnhancementBatchJob,
    EnhancementLevel,
    ImageEnhancementResult,
)
from operations.jobs import STALE_REASON, queue_status, reap_stuck_jobs
from uploads.models import (
    BatchImageProgress,
    ImagePreprocessingResult,
    ImageUpload,
    UploadBatch,
)
from uploads.progress import sync_image_progress

User = get_user_model()


@override_settings(JOB_PROCESSING_STALE_SECONDS=3600, JOB_QUEUED_STALE_HOURS=6)
class StuckJobReaperTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="reaper-owner", email="reaper@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="a.png", status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=self.image, status="completed", output_format="png",
        )

    def make_result(self, status, age):
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO, status=status,
        )
        # updated_at is auto_now, so age the row with a queryset update.
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            updated_at=timezone.now() - age
        )
        return result

    def test_stale_processing_job_is_failed_with_a_reason(self):
        result = self.make_result("processing", timedelta(hours=2))

        summary = reap_stuck_jobs()

        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_reason, STALE_REASON)
        self.assertIsNotNone(result.finished_at)
        self.assertEqual(summary["by_kind"], {"enhancement": 1})

    def test_a_shorter_allowance_can_be_given_for_one_run_after_a_restart(self):
        result = self.make_result("processing", timedelta(minutes=10))
        self.assertEqual(reap_stuck_jobs()["total"], 0)                          # not stale by the configured hour
        self.assertEqual(reap_stuck_jobs(dry_run=True, processing_seconds=120)["total"], 1)
        result.refresh_from_db()
        self.assertEqual(result.status, "processing")                            # a dry run changes nothing
        self.assertEqual(reap_stuck_jobs(processing_seconds=120)["total"], 1)
        result.refresh_from_db()
        self.assertEqual((result.status, result.failure_reason), ("failed", STALE_REASON))

    def test_the_command_takes_the_same_allowance(self):
        from io import StringIO

        from django.core.management import call_command

        result = self.make_result("processing", timedelta(minutes=10))
        out = StringIO()
        call_command("reap_stuck_jobs", "--older-than-seconds", "120", stdout=out)
        self.assertIn("Stuck jobs failed: 1", out.getvalue())
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")

    def test_recent_processing_job_is_left_alone(self):
        result = self.make_result("processing", timedelta(minutes=30))

        self.assertEqual(reap_stuck_jobs()["total"], 0)

        result.refresh_from_db()
        self.assertEqual(result.status, "processing")

    def test_queued_jobs_get_a_longer_allowance_than_processing_jobs(self):
        waiting = self.make_result("queued", timedelta(hours=2))

        self.assertEqual(reap_stuck_jobs()["total"], 0)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "queued")

        ImageEnhancementResult.objects.filter(pk=waiting.pk).update(
            updated_at=timezone.now() - timedelta(hours=7)
        )
        self.assertEqual(reap_stuck_jobs()["total"], 1)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "failed")

    def test_finished_jobs_are_never_touched(self):
        for status in ("completed", "failed", "cancelled"):
            result = self.make_result(status, timedelta(days=3))
            self.assertEqual(reap_stuck_jobs()["total"], 0)
            result.refresh_from_db()
            self.assertEqual(result.status, status)
            result.delete()

    def test_dry_run_reports_without_changing_anything(self):
        result = self.make_result("processing", timedelta(hours=2))

        summary = reap_stuck_jobs(dry_run=True)

        self.assertEqual(summary["total"], 1)
        result.refresh_from_db()
        self.assertEqual(result.status, "processing")

    def test_second_run_finds_nothing(self):
        self.make_result("processing", timedelta(hours=2))

        self.assertEqual(reap_stuck_jobs()["total"], 1)
        self.assertEqual(reap_stuck_jobs()["total"], 0)

    def test_user_can_retry_after_a_stuck_job_is_reaped(self):
        """The reason this exists: a dead active row blocks the image forever."""
        self.make_result("processing", timedelta(hours=2))
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImageEnhancementResult.objects.create(
                owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
                requested_level=EnhancementLevel.HIGH,
            )

        reap_stuck_jobs()

        retry = ImageEnhancementResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.HIGH,
        )
        self.assertEqual(retry.status, "pending")

    def test_reaping_resyncs_batch_progress(self):
        self.make_result("processing", timedelta(hours=2))
        sync_image_progress(self.image.pk)
        self.assertEqual(BatchImageProgress.objects.get(image=self.image).status, "processing")

        reap_stuck_jobs()

        self.assertEqual(BatchImageProgress.objects.get(image=self.image).status, "failed")

    def test_other_job_kinds_are_covered(self):
        old = timezone.now() - timedelta(hours=2)
        UploadBatch.objects.filter(pk=self.batch.pk).update(
            processing_status="processing", updated_at=old,
        )
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="processing",
        )
        EnhancementBatchJob.objects.filter(pk=job.pk).update(updated_at=old)
        package = DownloadPackage.objects.create(
            owner=self.user, batch=self.batch, package_type="accepted_results",
            status="generating",
        )
        DownloadPackage.objects.filter(pk=package.pk).update(updated_at=old)

        summary = reap_stuck_jobs()

        self.assertEqual(
            summary["by_kind"],
            {"enhancement_batch": 1, "upload_batch": 1, "download_package": 1},
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.processing_status, "failed")
        self.assertEqual(self.batch.processing_error, STALE_REASON)
        self.assertIsNotNone(self.batch.processing_finished_at)
        package.refresh_from_db()
        self.assertEqual(package.status, "failed")

    def test_management_command_supports_dry_run(self):
        self.make_result("processing", timedelta(hours=2))

        dry = StringIO()
        call_command("reap_stuck_jobs", "--dry-run", stdout=dry)
        self.assertIn("would fail: 1", dry.getvalue())

        real = StringIO()
        call_command("reap_stuck_jobs", stdout=real)
        self.assertIn("failed: 1", real.getvalue())


@override_settings(JOB_PROCESSING_STALE_SECONDS=3600, JOB_QUEUED_STALE_HOURS=6)
class QueueStatusTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="queue-admin", email="queue-admin@example.com",
            password="ForestRiver!4821", is_email_verified=True, role="admin",
        )
        self.user = User.objects.create_user(
            username="queue-user", email="queue-user@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        image = ImageUpload.objects.create(
            owner=self.user, batch=batch, original_name="a.png", status="uploaded",
        )
        pre = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="png",
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=pre, status="processing",
        )
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            updated_at=timezone.now() - timedelta(hours=3)
        )

    def test_snapshot_counts_active_and_stale_work(self):
        snapshot = queue_status()

        enhancement = snapshot["jobs"]["enhancement"]
        self.assertEqual(enhancement["processing"], 1)
        self.assertEqual(enhancement["queued"], 0)
        self.assertEqual(enhancement["stale"], 1)
        self.assertGreaterEqual(enhancement["oldest_active_age_seconds"], 0)
        self.assertEqual(snapshot["jobs"]["report"]["stale"], 0)
        self.assertIsNone(snapshot["jobs"]["report"]["oldest_active_age_seconds"])

    def test_endpoint_is_admin_only(self):
        client = APIClient()
        url = reverse("operations:queue-status")
        self.assertEqual(client.get(url).status_code, 401)
        client.force_authenticate(self.user)
        self.assertEqual(client.get(url).status_code, 403)
        client.force_authenticate(self.admin)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["jobs"]["enhancement"]["stale"], 1)

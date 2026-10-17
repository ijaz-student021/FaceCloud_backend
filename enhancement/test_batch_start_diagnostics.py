"""Starting a batch enhancement: explicit conflicts, logged failures, no partial jobs."""
from unittest.mock import patch

from django.db import IntegrityError
from django.test import override_settings
from rest_framework.test import APIClient

from .models import EnhancementBatchJob, ImageEnhancementResult
from .test_pipeline_regressions import NO_MODELS, ResultFixture


@override_settings(**NO_MODELS)
class BatchStartDiagnosticsTests(ResultFixture):
    def setUp(self):
        super().setUp()
        self.first = self.make_result("1.jpg", status="completed")
        self.second = self.make_result("2.jpg", status="completed")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/enhancements/batches/{self.batch.pk}/"

    def post(self):
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            return self.client.post(self.url, {"level": "auto"}, format="json")

    def test_a_ready_batch_starts(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(EnhancementBatchJob.objects.get().total_images, 2)

    def test_busy_images_are_named_and_nothing_is_created(self):
        busy = self.make_result("3.jpg", status="processing")
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["image_ids"], [busy.image_id])
        self.assertFalse(EnhancementBatchJob.objects.exists())

    def test_a_race_on_the_active_constraint_is_a_conflict_too(self):
        with patch("enhancement.batch_jobs.create_enhancement_result", side_effect=IntegrityError("duplicate key")):
            with self.assertLogs("enhancement.batch_jobs", level="WARNING"):
                response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(EnhancementBatchJob.objects.exists())

    def test_an_unexpected_error_is_logged_in_full_and_shown_only_as_a_reference(self):
        secret = "connection string postgres://admin:hunter2@db"
        with patch("enhancement.batch_jobs.create_enhancement_result", side_effect=RuntimeError(secret)):
            with self.assertLogs("enhancement.batch_jobs", level="ERROR") as logged:
                response = self.post()
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertNotIn("hunter2", str(body))
        self.assertEqual(len(body["error_reference"]), 12)
        self.assertIn(body["error_reference"], logged.output[0])
        self.assertIn("hunter2", "\n".join(logged.output))  # the traceback is kept server-side
        self.assertFalse(EnhancementBatchJob.objects.exists())
        self.assertEqual(ImageEnhancementResult.objects.filter(batch_job__isnull=False).count(), 0)

    def test_a_queue_failure_is_logged_and_the_job_marked_failed(self):
        with patch("enhancement.tasks.process_enhancement_batch.apply_async", side_effect=OSError("redis down")):
            with self.assertLogs("enhancement.batch_jobs", level="ERROR"):
                response = self.client.post(self.url, {"level": "auto"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "failed")

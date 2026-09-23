from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import EnhancementBatchJob, ImageEnhancementResult
from .tasks import process_enhancement_batch

User = get_user_model()


class BatchFailureIsolationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="batch-failure", email="batch-failure@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        self.job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=batch, status="queued", total_images=2,
        )
        self.results = []
        for index in range(2):
            image = ImageUpload.objects.create(
                owner=self.user, batch=batch, original_name=f"{index}.png", status="uploaded",
            )
            pre = ImagePreprocessingResult.objects.create(
                owner=self.user, image=image, status="completed", output_format="png",
            )
            self.results.append(ImageEnhancementResult.objects.create(
                owner=self.user, image=image, preprocessing_result=pre,
                batch_job=self.job, status="queued",
            ))

    def test_an_unexpected_error_is_logged_marked_failed_and_does_not_stop_the_batch(self):
        def fake(result):
            if result.pk == self.results[0].pk:
                raise RuntimeError("unexpected bug")
            result.status = "completed"
            result.save(update_fields=["status"])
            return result

        with patch("enhancement.tasks.process_enhancement_result", side_effect=fake):
            with self.assertLogs("enhancement.tasks", level="ERROR") as logged:
                outcome = process_enhancement_batch.run(self.job.pk)

        first, second = (ImageEnhancementResult.objects.get(pk=r.pk) for r in self.results)
        self.assertEqual(first.status, "failed")
        self.assertTrue(first.failure_reason)
        self.assertIsNotNone(first.finished_at)
        self.assertEqual(second.status, "completed")
        self.assertIn(f"result {first.pk}", logged.output[0])
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual((outcome["successful"], outcome["failed"]), (1, 1))

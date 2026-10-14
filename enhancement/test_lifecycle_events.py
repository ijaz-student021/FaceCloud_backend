"""Batch outcomes and events when cancellation or retries arrive late.

Mocked queue (tasks run in-process), real classical enhancement on tiny images.
"""
from unittest.mock import patch

from django.test import override_settings

from activity.models import ActivityEvent
from notifications.models import UserNotification
from uploads.models import BatchImageProgress, UploadBatch
from uploads.services import cancel_batch

from .models import EnhancementBatchJob, ImageEnhancementResult
from .services import enhance_bgr, process_enhancement_result
from .tasks import _publish_outcome, process_enhancement_batch
from .test_pipeline_regressions import NO_MODELS, ResultFixture


@override_settings(**NO_MODELS)
class LateCancellationTests(ResultFixture):
    def job_with(self, count):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=count,
        )
        return job, [self.make_result(f"{index}.jpg", job=job) for index in range(count)]

    def test_a_cancellation_with_nothing_left_to_cancel_keeps_the_real_outcome(self):
        """P2-10: every image was enhanced, then the user pressed cancel. (A
        cancel that stops the last image still closes the job as cancelled:
        test_staged_pipeline covers that.)"""
        job, results = self.job_with(2)
        for row in results:
            process_enhancement_result(row)
        UploadBatch.objects.filter(pk=self.batch.pk).update(cancellation_requested=True)

        outcome = process_enhancement_batch.run(job.pk)

        job.refresh_from_db()
        self.assertEqual((outcome["status"], job.status, job.successful_images), ("completed", "completed", 2))

    def test_a_cancellation_with_images_still_to_do_cancels_them(self):
        job, results = self.job_with(3)
        calls = []

        def cancel_during_the_first(*args, **kwargs):
            calls.append(1)
            UploadBatch.objects.filter(pk=self.batch.pk).update(cancellation_requested=True)
            return enhance_bgr(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=cancel_during_the_first):
            outcome = process_enhancement_batch.run(job.pk)

        job.refresh_from_db()
        self.assertEqual((outcome["status"], job.status), ("cancelled", "cancelled"))
        self.assertEqual(len(calls), 1)
        self.assertEqual({ImageEnhancementResult.objects.get(pk=row.pk).status for row in results}, {"cancelled"})

    def test_cancelling_marks_unfinished_progress_cancelled_and_leaves_finished_progress(self):
        """Confirms the audit correction: progress does not depend on the bulk update."""
        job, results = self.job_with(2)
        with self.captureOnCommitCallbacks(execute=True):  # progress syncs after commit
            process_enhancement_result(results[0])
        cancel_batch(self.batch)

        progress = {row.image_id: row.status for row in BatchImageProgress.objects.filter(batch=self.batch)}
        self.assertEqual(progress[results[1].image_id], "cancelled")
        self.assertNotEqual(progress[results[0].image_id], "cancelled")


@override_settings(**NO_MODELS)
class PublishedOnceTests(ResultFixture):
    def test_publishing_a_finished_result_again_adds_no_second_activity_entry(self):
        """P2-12: a retried task re-published completed results."""
        result = process_enhancement_result(self.make_result())
        _publish_outcome(result)
        _publish_outcome(result)

        self.assertEqual(ActivityEvent.objects.filter(
            user=self.user, event="enhancement_completed", object_type="ImageEnhancementResult",
            object_id=str(result.pk)).count(), 1)
        self.assertEqual(UserNotification.objects.filter(
            recipient=self.user, idempotency_key=f"image:{result.image_id}:enhancement_completed").count(), 1)

    def test_a_new_outcome_for_the_same_result_is_still_recorded(self):
        result = process_enhancement_result(self.make_result())
        _publish_outcome(result)
        ImageEnhancementResult.objects.filter(pk=result.pk).update(status="failed")
        result.refresh_from_db()
        _publish_outcome(result)

        events = ActivityEvent.objects.filter(
            user=self.user, object_type="ImageEnhancementResult", object_id=str(result.pk),
        )
        self.assertEqual(sorted(events.values_list("event", flat=True)), ["enhancement_completed", "enhancement_failed"])

    def test_an_unfinished_result_is_not_published(self):
        result = self.make_result(status="processing")
        _publish_outcome(result)
        self.assertFalse(ActivityEvent.objects.filter(user=self.user).exists())

"""A task's own retry must resume its claim, never be locked out by it.

Mocked: the enhancement itself is real OpenCV code on a tiny image with every
model switched off; the database failure is injected. No model runs.
"""
from unittest.mock import patch

from celery.exceptions import Retry
from django.db.utils import OperationalError
from django.test import override_settings
from django.utils import timezone

from .models import EnhancementBatchJob, ImageEnhancementResult
from .services import ResultInProgress, enhance_bgr, process_enhancement_result
from .tasks import MAX_CLAIM_WAITS, process_enhancement_batch, process_enhancement_image
from .test_pipeline_regressions import NO_MODELS, ResultFixture


def failing_then_real(failures, error=None):
    """An ``enhance_bgr`` that drops the database connection ``failures`` times."""
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) <= failures:
            raise error or OperationalError("server closed the connection unexpectedly")
        return enhance_bgr(*args, **kwargs)

    return run, calls


@override_settings(**NO_MODELS)
class RetryAfterClaimTests(ResultFixture):
    def test_a_database_failure_after_the_claim_is_retried_to_completion(self):
        """The regression: the retry used to find its own claim and give up."""
        result = self.make_result()
        run, calls = failing_then_real(1)

        with patch("enhancement.services.enhance_bgr", side_effect=run):
            outcome = process_enhancement_image.apply(args=[result.pk]).get()

        result.refresh_from_db()
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual((result.status, result.claim_token), ("completed", ""))
        self.assertTrue(result.enhanced_file)

    def test_the_claim_really_was_held_when_the_database_failed(self):
        """Guards the test above: the failure happens after the claim, not before."""
        result = self.make_result()
        seen = []

        def fail_once_recording_the_row(*args, **kwargs):
            row = ImageEnhancementResult.objects.get(pk=result.pk)
            seen.append((row.status, bool(row.claim_token), row.claim_attempt))
            if len(seen) == 1:
                raise OperationalError("server closed the connection unexpectedly")
            return enhance_bgr(*args, **kwargs)

        with patch("enhancement.services.enhance_bgr", side_effect=fail_once_recording_the_row):
            process_enhancement_image.apply(args=[result.pk])

        self.assertEqual(seen, [("processing", True, 0), ("processing", True, 1)])

    def test_every_allowed_retry_resumes_and_the_last_one_records_the_failure(self):
        result = self.make_result()
        run, calls = failing_then_real(99)

        with patch("enhancement.services.enhance_bgr", side_effect=run):
            with self.assertLogs("enhancement.tasks", level="ERROR"):
                outcome = process_enhancement_image.apply(args=[result.pk]).get()

        result.refresh_from_db()
        self.assertEqual(len(calls), 1 + process_enhancement_image.max_retries)  # none was locked out
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual((result.status, result.claim_token), ("failed", ""))

    def test_a_batch_retry_resumes_the_image_that_was_mid_flight(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=2,
        )
        results = [self.make_result(f"{index}.jpg", job=job) for index in range(2)]
        run, calls = failing_then_real(1)

        with patch("enhancement.services.enhance_bgr", side_effect=run):
            process_enhancement_batch.apply(args=[job.pk])

        job.refresh_from_db()
        self.assertEqual(len(calls), 3)  # image 1 twice, image 2 once
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            [ImageEnhancementResult.objects.get(pk=row.pk).status for row in results], ["completed", "completed"],
        )


@override_settings(**NO_MODELS)
class ClaimOwnershipRuleTests(ResultFixture):
    def held_by(self, task_id, attempt):
        result = self.make_result(status="processing")
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            claim_token="a" * 32, claimed_at=timezone.now(), claim_task_id=task_id, claim_attempt=attempt,
        )
        return result

    def test_a_later_attempt_of_the_same_task_resumes(self):
        result = self.held_by("task-1", 0)
        self.assertEqual(process_enhancement_result(result, claim_owner=("task-1", 1)).status, "completed")

    def test_a_second_delivery_of_the_same_attempt_does_not(self):
        """Same id, same attempt: the first delivery may still be running."""
        result = self.held_by("task-1", 0)
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result, claim_owner=("task-1", 0))

    def test_an_earlier_attempt_cannot_take_a_later_attempts_claim(self):
        result = self.held_by("task-1", 2)
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result, claim_owner=("task-1", 1))

    def test_a_different_task_does_not_resume_whatever_its_attempt_number(self):
        result = self.held_by("task-1", 0)
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result, claim_owner=("task-2", 3))

    def test_a_caller_with_no_task_identity_never_resumes(self):
        result = self.held_by("", 0)
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result)
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result, claim_owner=("", 1))


@override_settings(**NO_MODELS)
class WaitingOnAnotherWorkerTests(ResultFixture):
    """With a real queue, a task that meets someone else's claim stays scheduled."""

    def claim_elsewhere(self, result):
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            status="processing", claim_token="a" * 32, claimed_at=timezone.now(),
            claim_task_id="another-task", claim_attempt=0,
        )

    def run_on_a_queue(self, result, retries):
        """Run the task body as a worker would: not eager, not called directly."""
        task = process_enhancement_image
        task.push_request(id="this-task", retries=retries, is_eager=False, called_directly=False)
        try:
            with patch.object(task, "retry", side_effect=Retry("scheduled")) as retry:
                try:
                    return task.run(result.pk), retry
                except Retry:
                    return "rescheduled", retry
        finally:
            task.pop_request()

    def test_it_reschedules_itself_for_when_the_other_claim_lapses(self):
        result = self.make_result()
        self.claim_elsewhere(result)

        outcome, retry = self.run_on_a_queue(result, retries=0)

        self.assertEqual(outcome, "rescheduled")
        self.assertGreater(retry.call_args.kwargs["countdown"], 0)
        result.refresh_from_db()
        self.assertEqual((result.status, result.failure_reason), ("processing", ""))  # untouched

    def test_having_already_retried_for_the_database_does_not_stop_it_waiting(self):
        """The second half of the regression: retries > 0 used to mean "give up"."""
        result = self.make_result()
        self.claim_elsewhere(result)

        outcome, retry = self.run_on_a_queue(result, retries=process_enhancement_image.max_retries)

        self.assertEqual(outcome, "rescheduled")
        self.assertGreater(retry.call_args.kwargs["max_retries"], process_enhancement_image.max_retries)

    def test_waiting_is_bounded(self):
        result = self.make_result()
        self.claim_elsewhere(result)
        exhausted = process_enhancement_image.max_retries + MAX_CLAIM_WAITS

        with self.assertLogs("enhancement.tasks", level="WARNING"):
            outcome, retry = self.run_on_a_queue(result, retries=exhausted)

        self.assertEqual(outcome["status"], "in_progress")
        retry.assert_not_called()
        result.refresh_from_db()
        self.assertEqual(result.status, "processing")  # still the other worker's, not failed

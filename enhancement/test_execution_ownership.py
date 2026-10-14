"""Which execution owns a result, and what each execution may write.

Mocked: the enhancement is real OpenCV code on a tiny image with every model
switched off; queue behaviour (redelivery, waiting, retrying) is simulated by
running the task body as a worker would, with ``retry`` intercepted. The real
Redis/Celery/PostgreSQL behaviour is exercised by
``scripts/check_queue_integration.py``.
"""
import time
from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import Retry
from django.conf import settings
from django.db.utils import OperationalError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import User
from uploads.models import UploadBatch

from .models import EnhancementBatchJob, ImageEnhancementResult
from .services import ClaimRenewal, ResultInProgress, enhance_bgr, process_enhancement_result
from .tasks import MAX_CLAIM_WAITS, process_enhancement_batch, process_enhancement_image
from .test_pipeline_regressions import NO_MODELS, ResultFixture

LIVE_TOKEN = "a" * 32


def run_as_worker(task, *args, retries=0, task_id="this-task", **kwargs):
    """Run a task body as a queue worker would: not eager, not called directly.

    Returns ``(outcome, retry_mock)``; ``outcome`` is ``"rescheduled"`` when the
    task asked the queue to run it again.
    """
    task.push_request(id=task_id, retries=retries, is_eager=False, called_directly=False)
    try:
        with patch.object(task, "retry", side_effect=Retry("scheduled")) as retry:
            try:
                return task.run(*args, **kwargs), retry
            except Retry:
                return "rescheduled", retry
    finally:
        task.pop_request()


class OwnershipFixture(ResultFixture):
    def hold(self, result, token=LIVE_TOKEN, task_id="this-task", attempt=0, age=0):
        ImageEnhancementResult.objects.filter(pk=result.pk).update(
            status="processing", claim_token=token, claimed_at=timezone.now() - timedelta(seconds=age),
            claim_task_id=task_id, claim_attempt=attempt,
        )
        result.refresh_from_db()
        return result


@override_settings(**NO_MODELS)
class DuplicateCannotTakeOverTests(OwnershipFixture):
    def test_a_claim_wait_retry_does_not_take_over_a_live_claim(self):
        """P1-2. The second delivery of attempt 0 waited once (retries=1). Its
        retry number is now higher than the live claim's attempt, which used to
        be read as "the earlier attempt has ended" and let it take over."""
        result = self.hold(self.make_result(), task_id="this-task", attempt=0)

        outcome, retry = run_as_worker(process_enhancement_image, result.pk, retries=1, claim_waits=1)

        self.assertEqual(outcome, "rescheduled")
        result.refresh_from_db()
        self.assertEqual((result.status, result.claim_token), ("processing", LIVE_TOKEN))
        self.assertFalse(result.enhanced_file)

    def test_a_higher_attempt_number_alone_never_resumes(self):
        result = self.hold(self.make_result(), task_id="task-1", attempt=0)
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result, claim_owner=("task-1", 5))

    def test_the_execution_that_held_the_claim_can_resume_it(self):
        """A retry carries the token its ended execution held."""
        result = self.hold(self.make_result(), task_id="task-1", attempt=0)
        resumed = process_enhancement_result(result, claim_owner=("task-1", 1), resume_token=LIVE_TOKEN)
        self.assertEqual(resumed.status, "completed")

    def test_a_wrong_resume_token_does_not_resume(self):
        result = self.hold(self.make_result())
        with self.assertRaises(ResultInProgress):
            process_enhancement_result(result, claim_owner=("this-task", 1), resume_token="b" * 32)

    def test_the_wait_lasts_until_the_claim_lapses_and_never_longer_than_the_lease(self):
        result = self.hold(self.make_result())
        with self.assertRaises(ResultInProgress) as raised:
            process_enhancement_result(result)
        self.assertLessEqual(raised.exception.retry_after, settings.ENHANCEMENT_CLAIM_LEASE_SECONDS)
        self.assertGreaterEqual(raised.exception.retry_after, settings.ENHANCEMENT_CLAIM_LEASE_SECONDS - 5)


@override_settings(**NO_MODELS)
class DatabaseFailureAfterClaimTests(OwnershipFixture):
    def test_the_retry_carries_the_claim_it_held(self):
        result = self.make_result()

        def drop_connection(*args, **kwargs):
            raise OperationalError("server closed the connection unexpectedly")

        with patch("enhancement.services.enhance_bgr", side_effect=drop_connection):
            outcome, retry = run_as_worker(process_enhancement_image, result.pk)

        self.assertEqual(outcome, "rescheduled")
        held = ImageEnhancementResult.objects.get(pk=result.pk).claim_token
        self.assertTrue(held)
        self.assertEqual(retry.call_args.kwargs["kwargs"]["resume_claim"], held)

    def test_claim_waits_do_not_use_up_the_database_retries(self):
        """P2-8. Two claim waits and one database retry have happened
        (retries=3); a database failure must still be retried."""
        result = self.make_result()

        with patch("enhancement.services.enhance_bgr", side_effect=OperationalError("gone")):
            outcome, retry = run_as_worker(
                process_enhancement_image, result.pk, retries=MAX_CLAIM_WAITS + 1, claim_waits=MAX_CLAIM_WAITS,
            )

        self.assertEqual(outcome, "rescheduled")
        self.assertEqual(ImageEnhancementResult.objects.get(pk=result.pk).status, "processing")

    def test_database_retries_are_still_bounded(self):
        result = self.make_result()

        with patch("enhancement.services.enhance_bgr", side_effect=OperationalError("gone")):
            with self.assertLogs("enhancement.tasks", level="ERROR"):
                outcome, retry = run_as_worker(
                    process_enhancement_image, result.pk, retries=process_enhancement_image.max_retries,
                )

        self.assertEqual(outcome["status"], "failed")
        retry.assert_not_called()

    def test_claim_waits_are_bounded_whatever_the_database_retries(self):
        result = self.hold(self.make_result(), token=LIVE_TOKEN, task_id="another-task")

        with self.assertLogs("enhancement.tasks", level="WARNING"):
            outcome, retry = run_as_worker(
                process_enhancement_image, result.pk, retries=MAX_CLAIM_WAITS, claim_waits=MAX_CLAIM_WAITS,
            )

        self.assertEqual(outcome["status"], "in_progress")
        retry.assert_not_called()


@override_settings(**NO_MODELS)
class OldWorkerFailureTests(OwnershipFixture):
    def taken_over_then_crash(self, result):
        """An enhancement during which another execution takes the result over
        (this one's lease lapsed), after which this one crashes."""

        def run(*args, **kwargs):
            ImageEnhancementResult.objects.filter(pk=result.pk).update(
                claim_token=LIVE_TOKEN, claimed_at=timezone.now(), claim_task_id="new-owner",
            )
            raise RuntimeError("out of memory")

        return run

    def test_a_crash_after_losing_the_claim_does_not_fail_the_new_owners_row(self):
        """P1-3."""
        result = self.make_result()

        with patch("enhancement.services.enhance_bgr", side_effect=self.taken_over_then_crash(result)), \
                patch("enhancement.tasks._publish_outcome") as publish:
            with self.assertLogs("enhancement", level="ERROR"):
                run_as_worker(process_enhancement_image, result.pk)

        result.refresh_from_db()
        self.assertEqual((result.status, result.claim_token), ("processing", LIVE_TOKEN))
        publish.assert_not_called()

    def test_a_crash_while_still_the_owner_records_the_failure(self):
        result = self.make_result()

        with patch("enhancement.services.enhance_bgr", side_effect=RuntimeError("out of memory")):
            with self.assertLogs("enhancement", level="ERROR"):
                outcome, _ = run_as_worker(process_enhancement_image, result.pk)

        result.refresh_from_db()
        self.assertEqual((outcome["status"], result.status, result.claim_token), ("failed", "failed", ""))

    def test_a_failed_batch_does_not_fail_images_another_worker_holds(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=2,
        )
        first = self.make_result("1.jpg", job=job)
        second = self.hold(self.make_result("2.jpg", job=job), task_id="another-task")

        from celery.exceptions import SoftTimeLimitExceeded
        with patch("enhancement.services.enhance_bgr", side_effect=SoftTimeLimitExceeded()):
            with self.assertLogs("enhancement", level="ERROR"):
                run_as_worker(process_enhancement_batch, job.pk)

        self.assertEqual(ImageEnhancementResult.objects.get(pk=first.pk).status, "failed")
        second.refresh_from_db()
        self.assertEqual((second.status, second.claim_token), ("processing", LIVE_TOKEN))

    def test_a_lapsed_claim_with_no_live_owner_can_be_closed(self):
        """A row left by a dead worker is not "someone else's live work"."""
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued", total_images=2,
        )
        self.make_result("1.jpg", job=job)
        stale = self.hold(self.make_result("2.jpg", job=job), task_id="dead-task",
                          age=settings.ENHANCEMENT_CLAIM_LEASE_SECONDS + 5)

        from celery.exceptions import SoftTimeLimitExceeded
        with patch("enhancement.services.enhance_bgr", side_effect=SoftTimeLimitExceeded()):
            with self.assertLogs("enhancement", level="ERROR"):
                run_as_worker(process_enhancement_batch, job.pk)

        self.assertEqual(ImageEnhancementResult.objects.get(pk=stale.pk).status, "failed")


@override_settings(**NO_MODELS)
class BatchRedeliveryTests(OwnershipFixture):
    def test_a_redelivered_batch_comes_back_for_the_image_a_dead_worker_held(self):
        """P2-9. The batch is redelivered (same id, same attempt) while the dead
        worker's claim on image 2 is still within its lease."""
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="processing", total_images=3,
            task_id="this-task",
        )
        first = self.make_result("1.jpg", job=job)
        second = self.hold(self.make_result("2.jpg", job=job), task_id="this-task", attempt=0)
        third = self.make_result("3.jpg", job=job)

        outcome, retry = run_as_worker(process_enhancement_batch, job.pk)

        self.assertEqual(outcome, "rescheduled")
        self.assertGreater(retry.call_args.kwargs["countdown"], 0)
        self.assertEqual(retry.call_args.kwargs["kwargs"]["claim_waits"], 1)
        statuses = [ImageEnhancementResult.objects.get(pk=row.pk).status for row in (first, second, third)]
        self.assertEqual(statuses, ["completed", "processing", "completed"])
        job.refresh_from_db()
        self.assertEqual(job.status, "processing")  # not finalised without image 2

        # The dead worker's lease lapses; the rescheduled batch takes image 2 over.
        ImageEnhancementResult.objects.filter(pk=second.pk).update(
            claimed_at=timezone.now() - timedelta(seconds=settings.ENHANCEMENT_CLAIM_LEASE_SECONDS + 1),
        )
        outcome, retry = run_as_worker(process_enhancement_batch, job.pk, retries=1, claim_waits=1)

        job.refresh_from_db()
        self.assertEqual((outcome["status"], job.status, job.successful_images), ("completed", "completed", 3))

    def test_a_batch_retry_does_not_redo_or_republish_finished_images(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="processing", total_images=2,
        )
        done = process_enhancement_result(self.make_result("1.jpg", job=job))
        self.make_result("2.jpg", job=job)

        with patch("enhancement.tasks._publish_outcome") as publish, \
                patch("enhancement.services.enhance_bgr", side_effect=enhance_bgr) as enhanced:
            run_as_worker(process_enhancement_batch, job.pk)

        self.assertEqual(enhanced.call_count, 1)
        self.assertEqual([call.args[0].pk for call in publish.call_args_list], [done.pk + 1])

    def test_waiting_for_another_workers_image_is_bounded(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="processing", total_images=2,
        )
        self.make_result("1.jpg", job=job)
        held = self.hold(self.make_result("2.jpg", job=job), task_id="another-task")

        with self.assertLogs("enhancement.tasks", level="WARNING"):
            outcome, retry = run_as_worker(
                process_enhancement_batch, job.pk, retries=MAX_CLAIM_WAITS, claim_waits=MAX_CLAIM_WAITS,
            )

        retry.assert_not_called()
        self.assertEqual(outcome["successful"], 1)
        self.assertEqual(ImageEnhancementResult.objects.get(pk=held.pk).status, "processing")


@override_settings(**NO_MODELS)
class ClaimRenewalTests(OwnershipFixture):
    def test_renewal_refreshes_only_a_claim_still_held(self):
        result = self.hold(self.make_result(), age=100)
        before = result.claimed_at

        self.assertTrue(ClaimRenewal(result.pk, LIVE_TOKEN).renew())
        result.refresh_from_db()
        self.assertGreater(result.claimed_at, before)

        self.assertFalse(ClaimRenewal(result.pk, "b" * 32).renew())

    def test_renewal_stops_once_the_supported_execution_window_has_passed(self):
        result = self.hold(self.make_result())
        renewal = ClaimRenewal(result.pk, LIVE_TOKEN)
        renewal.started -= settings.CELERY_TASK_TIME_LIMIT + 1
        self.assertFalse(renewal.renew())


@override_settings(**NO_MODELS)
class ClaimRenewedDuringALongStageTests(TransactionTestCase):
    """The renewal thread uses its own database connection, so the rows must be
    committed for it to see them."""

    def test_a_stage_longer_than_the_renewal_interval_keeps_the_claim_fresh(self):
        user = User.objects.create_user(
            username="renewal", email="renewal@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        fixture = ResultFixture()
        fixture.user, fixture.batch = user, UploadBatch.objects.create(owner=user, upload_type="multiple")
        result = fixture.make_result()
        seen = []

        def long_stage(*args, **kwargs):
            first = ImageEnhancementResult.objects.get(pk=result.pk).claimed_at
            time.sleep(0.6)  # no checkpoint in here: only the renewal can refresh the claim
            seen.append((first, ImageEnhancementResult.objects.get(pk=result.pk).claimed_at))
            return enhance_bgr(*args, **kwargs)

        with patch("enhancement.services.CLAIM_RENEWAL_INTERVAL_SECONDS", 0.1), \
                patch("enhancement.services.enhance_bgr", side_effect=long_stage):
            finished = process_enhancement_result(result)

        self.assertEqual(finished.status, "completed")
        first, later = seen[0]
        self.assertGreater(later, first)

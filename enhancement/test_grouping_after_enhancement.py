"""Person folders come AFTER the automatic enhancement (identities/verdict.py ``group_when_enhancement_ends``), never before it and never forever late.

The photographs go through the real path (``Base``: the quality task decides, the enhancement it queues is held back with ``execute=False`` or run with the real pipeline). The grouping
itself is replaced by a recorder that notes what state the photograph's enhancement was in at the instant grouping was asked for: what is checked is WHEN, which is all that moved.
"""
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone

from identities.models import IdentityAnalysis
from identities.verdict import WAIT_FOR_ENHANCEMENT, batch_grouping_finished, grouping_status
from uploads.models import BatchImageProgress
from uploads.progress import STAGE_ORDER, STAGE_PROGRESS, batch_progress_payload, sync_image_progress

from .auto import enhancement_pending, notify_batch_if_finished, release_overdue_grouping, release_waiting
from .models import AutoEnhancement, EnhancementStatus, EnhancementTrigger, ImageEnhancementResult
from .tasks import _publish_outcome
from .test_auto_enhancement import Base, picture

FINISHED = ("completed", "failed", "cancelled")


@override_settings(IDENTITY_AUTO_ANALYZE=True)
class GroupingFollowsTheEnhancementTests(Base):
    def setUp(self):
        super().setUp()
        self.asked = []            # (image id, the state of its latest enhancement when grouping was asked for)
        patcher = patch("identities.verdict.group_after_verdict", side_effect=self.record)
        self.group = patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, image, verdict=None):
        from identities.verdict import verdict_allows_grouping
        from uploads.models import ImageUpload

        fresh = ImageUpload.objects.get(pk=image.pk)
        if not verdict_allows_grouping(fresh, verdict)[0]:               # what the real grouping would refuse (a team photograph before its moderator) is not "asked for"
            return None
        latest = ImageEnhancementResult.objects.filter(image_id=image.pk).order_by("-created_at").first()
        self.asked.append((image.pk, None if latest is None else latest.status))
        return None

    def analysis(self, image, status):
        return IdentityAnalysis.objects.create(owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status=status)

    def asked_for(self, image):
        return [state for pk, state in self.asked if pk == image.pk]

    # -- the order -----------------------------------------------------------------------------------------------------------------
    def test_grouping_waits_for_a_running_enhancement_and_is_asked_for_when_it_completes(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)                           # decided, the enhancement is queued and has not run
        result = self.results(image).get()
        self.assertEqual(result.status, "queued")
        self.assertTrue(enhancement_pending(image))
        self.assertEqual(self.asked_for(image), [], "grouping must not start while the enhancement is still to come")
        self.run_queued(result)
        self.assertEqual(self.results(image).get().status, "completed")
        self.assertEqual(self.asked_for(image), ["completed"])                   # exactly once, and only once the enhancement had ended
        self.assertFalse(enhancement_pending(image))

    def test_a_failed_enhancement_releases_the_grouping(self):
        image = self.photo()
        with patch("enhancement.tasks.process_enhancement_result", side_effect=RuntimeError("model fell over")):
            self.decide(image, "accepted")
        self.assertEqual(self.results(image).get().status, "failed")
        self.assertEqual(self.asked_for(image), ["failed"])

    def test_a_queue_that_is_down_releases_the_grouping(self):
        image = self.photo()
        with patch("enhancement.tasks.process_enhancement_image.apply_async", side_effect=RuntimeError("broker down")):
            self.decide(image, "accepted")
        self.assertEqual(self.asked_for(image), ["failed"])

    def test_a_cancelled_enhancement_releases_the_grouping(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)
        result = self.results(image).get()
        ImageEnhancementResult.objects.filter(pk=result.pk).update(status=EnhancementStatus.CANCELLED, finished_at=timezone.now())
        _publish_outcome(ImageEnhancementResult.objects.get(pk=result.pk))
        self.assertEqual(self.asked_for(image), ["cancelled"])

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1, AUTO_ENHANCEMENT_CAPACITY_WAIT_MINUTES=1)
    def test_an_enhancement_skipped_for_capacity_releases_the_grouping(self):
        first, second = self.photo(), self.photo()
        self.decide(first, "accepted", execute=False)
        self.decide(second, "accepted", execute=False)                         # over the owner's limit: waiting for a slot, no result yet
        self.assertEqual(AutoEnhancement.objects.get(image=second).state, "waiting")
        self.assertTrue(enhancement_pending(second))
        self.assertEqual(self.asked_for(second), [])
        AutoEnhancement.objects.filter(image=second).update(waiting_since=timezone.now() - timedelta(minutes=5))
        release_waiting()
        self.assertEqual(AutoEnhancement.objects.get(image=second).skip_reason, "capacity")
        self.assertEqual(self.asked_for(second), [None])                        # skipped: no enhancement to wait for, grouped at once

    # -- no enhancement is coming ---------------------------------------------------------------------------------------------------
    def test_grouping_starts_at_once_when_the_batch_did_not_ask_for_enhancement(self):
        self.batch.auto_enhance = False
        self.batch.save(update_fields=["auto_enhance"])
        image = self.photo()
        self.decide(image, "accepted")
        self.assertFalse(self.results(image).exists())
        self.assertEqual(self.asked_for(image), [None])

    @override_settings(AUTO_ENHANCEMENT_ENABLED=False)
    def test_grouping_starts_at_once_when_the_operator_switched_automatic_enhancement_off(self):
        image = self.photo()
        self.decide(image, "accepted")
        self.assertEqual(self.asked_for(image), [None])

    def test_a_person_with_automatic_enhancement_off_for_the_account_is_grouped_at_once(self):
        from notifications.models import NotificationPreference

        NotificationPreference.objects.update_or_create(user=self.owner, defaults={"auto_enhance": False})
        self.batch.auto_enhance = False
        self.batch.save(update_fields=["auto_enhance"])
        image = self.photo()
        self.decide(image, "accepted")
        self.assertEqual(self.asked_for(image), [None])

    @override_settings(QUALITY_CHECKS_REJECT=True)
    def test_a_rejected_photograph_is_neither_enhanced_nor_asked_to_be_grouped_by_the_verdict(self):
        image = self.photo()
        self.decide(image, "rejected")
        self.assertFalse(self.results(image).exists())
        self.assertFalse(AutoEnhancement.objects.filter(image=image, state="started").exists())

    # -- personal and team photographs ----------------------------------------------------------------------------------------------
    def test_a_personal_workspace_photograph_the_checks_doubted_goes_enhancement_then_grouping(self):
        image = self.photo()
        self.decide(image, "needs_review", execute=False)
        result = self.results(image).get()
        self.assertEqual(self.asked_for(image), [])
        self.run_queued(result)
        self.assertEqual(self.asked_for(image), ["completed"])

    def test_a_team_photograph_waits_for_its_moderator_and_then_goes_enhancement_then_grouping(self):
        organization, moderator = self.organization_batch()
        image = self.photo()
        self.decide(image, "needs_review", execute=False)
        self.assertFalse(self.results(image).exists(), "nothing is enhanced before the moderator decides")
        self.assertEqual(self.asked_for(image), [])
        with patch("enhancement.auto._dispatch"):
            self.approve(organization, moderator, image)                        # the approval queues the enhancement first ...
        result = self.results(image).get()
        self.assertEqual(result.status, "queued")
        self.assertEqual(self.asked_for(image), [])                             # ... and the person folders wait for it
        self.run_queued(result)
        self.assertEqual(self.asked_for(image), ["completed"])

    # -- a person's own request ----------------------------------------------------------------------------------------------------
    def test_a_persons_own_enhancement_request_neither_delays_nor_triggers_grouping(self):
        self.batch.auto_enhance = False
        self.batch.save(update_fields=["auto_enhance"])
        image = self.photo()
        manual = ImageEnhancementResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, requested_level="auto", requested_style="natural",
            status=EnhancementStatus.QUEUED, trigger=EnhancementTrigger.MANUAL,
        )
        self.decide(image, "accepted", execute=False)
        self.assertEqual(len(self.asked_for(image)), 1, "a manual request in progress must not delay grouping")
        before = len(self.asked)
        ImageEnhancementResult.objects.filter(pk=manual.pk).update(status=EnhancementStatus.COMPLETED, finished_at=timezone.now())
        _publish_outcome(ImageEnhancementResult.objects.get(pk=manual.pk))
        self.assertEqual(len(self.asked), before, "a manual result ending must not trigger grouping")

    # -- never ungrouped forever ---------------------------------------------------------------------------------------------------
    @override_settings(GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS=300)
    def test_a_lost_enhancement_stops_holding_the_grouping_after_the_maximum_wait(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)                           # the worker that should have run it died
        self.assertTrue(enhancement_pending(image))
        AutoEnhancement.objects.filter(image=image).update(created_at=timezone.now() - timedelta(seconds=400))
        self.assertFalse(enhancement_pending(image))
        self.assertEqual(release_overdue_grouping(), 1)                         # the scheduler's pass groups it
        self.assertEqual(self.asked_for(image), ["queued"])

    @override_settings(GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS=300)
    def test_the_overdue_pass_leaves_alone_a_photograph_still_inside_its_wait_and_one_already_grouped(self):
        waiting, grouped = self.photo(), self.photo()
        self.decide(waiting, "accepted", execute=False)
        self.decide(grouped, "accepted", execute=False)
        AutoEnhancement.objects.filter(image=grouped).update(created_at=timezone.now() - timedelta(seconds=400))
        self.analysis(grouped, "completed")
        self.assertEqual(release_overdue_grouping(), 0)

    def test_the_maximum_wait_has_a_floor_and_a_default(self):
        from django.conf import settings

        self.assertEqual(settings.GROUPING_MAX_WAIT_FOR_ENHANCEMENT_SECONDS, 900)

    # -- a consent that is missing ---------------------------------------------------------------------------------------------------
    @override_settings(BIOMETRIC_CONSENT_REQUIRED=True)
    def test_without_consent_the_enhancement_runs_and_grouping_is_refused_as_before_without_leaving_the_photograph_unfinished(self):
        self.group.side_effect = None
        patch.stopall()                                                         # the real grouping: it refuses without consent
        image = self.photo()
        self.decide(image, "accepted")
        self.assertEqual(self.results(image).get().status, "completed")
        self.assertFalse(IdentityAnalysis.objects.filter(image=image).exists())
        sync_image_progress(image.pk)
        self.assertEqual(BatchImageProgress.objects.get(image=image).stage, "completed")


@override_settings(IDENTITY_AUTO_ANALYZE=True)
class ProgressInTheNewOrderTests(Base):
    def setUp(self):
        super().setUp()
        patcher = patch("identities.verdict.group_after_verdict")
        patcher.start()
        self.addCleanup(patcher.stop)

    def analysis(self, image, status):
        return IdentityAnalysis.objects.create(owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status=status)

    def progress(self, image):
        sync_image_progress(image.pk)
        row = BatchImageProgress.objects.get(image=image)
        return row.stage, row.status, row.progress_percentage

    def test_the_stage_order_is_published_and_has_the_enhancement_before_the_person_folders(self):
        self.assertEqual(STAGE_ORDER, ["uploaded", "queued", "preprocessing", "face_validation", "quality_analysis", "enhancement", "identity_grouping", "finalizing", "completed"])
        payload = batch_progress_payload(self.batch)
        self.assertEqual(payload["stage_order"], STAGE_ORDER)
        self.assertEqual(len(payload["stage_order"]), 9)
        ranks = [STAGE_PROGRESS[stage] for stage in STAGE_ORDER]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual((STAGE_PROGRESS["enhancement"], STAGE_PROGRESS["identity_grouping"]), (75.0, 95.0))

    def test_the_stages_a_photograph_shows_follow_the_order_and_the_percentages_never_go_down(self):
        image = self.photo()
        seen = []
        self.decide(image, "accepted", execute=False)
        seen.append(self.progress(image))                                       # the enhancement is queued
        self.run_queued(self.results(image).get())                              # it completes; the person folders are about to be queued (the grouping is stubbed out here)
        seen.append(self.progress(image))                                       # never a finished photograph in between: it is already shown in the next stage
        self.analysis(image, "queued")
        seen.append(self.progress(image))
        IdentityAnalysis.objects.filter(image=image).update(status="processing")
        seen.append(self.progress(image))
        IdentityAnalysis.objects.filter(image=image).update(status="completed")
        seen.append(self.progress(image))
        self.assertEqual([stage for stage, _status, _percentage in seen], ["enhancement", "identity_grouping", "identity_grouping", "identity_grouping", "completed"])
        percentages = [percentage for _stage, _status, percentage in seen]
        self.assertEqual(percentages, [75.0, 90.0, 95.0, 95.0, 100.0])
        self.assertEqual(percentages, sorted(percentages), "the percentages never go down across the life of a photograph")

    def test_while_the_enhancement_runs_the_photograph_is_in_the_enhancement_stage_even_if_an_analysis_row_exists(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)
        self.analysis(image, "queued")     # a photograph already in flight when this was deployed
        self.assertEqual(self.progress(image)[:2], ("enhancement", "queued"))

    def test_a_photograph_with_an_analysis_and_no_enhancement_result_still_shows_a_sensible_stage(self):
        self.batch.auto_enhance = False
        self.batch.save(update_fields=["auto_enhance"])
        image = self.photo()
        self.decide(image, "accepted")
        self.analysis(image, "processing")
        self.assertEqual(self.progress(image)[:2], ("identity_grouping", "processing"))

    def test_a_photograph_waiting_for_its_enhancement_says_so_in_the_grouping_status(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)
        self.assertEqual(grouping_status(self.fresh(image), personal=True), {"status": "queued", "waiting_for": WAIT_FOR_ENHANCEMENT})
        self.assertFalse(batch_grouping_finished(self.batch))
        row = next(item for item in batch_progress_payload(self.batch)["images"] if item["image_id"] == image.pk)
        self.assertEqual(row["identity_grouping"], {"status": "queued", "waiting_for": "enhancement"})

    def test_the_batch_is_not_finished_and_the_owner_is_not_told_while_a_person_folder_is_still_being_made(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)
        self.analysis(image, "queued")                                          # what the end of the enhancement queues
        self.run_queued(self.results(image).get())                              # the enhancement completes; its bookkeeping looks at the batch
        self.assertIsNone(self.fresh_batch().auto_enhance_notified_at, "the owner is not told before the person folders are made")
        self.assertIsNone(notify_batch_if_finished(self.batch.pk))
        self.assertNotIn(batch_progress_payload(self.batch)["status"], ("completed", "partial"))
        IdentityAnalysis.objects.filter(image=image).update(status="completed")
        self.assertIsNotNone(notify_batch_if_finished(self.batch.pk))           # the last person folder ends it (identities/tasks.py asks again)
        self.assertIsNotNone(self.fresh_batch().auto_enhance_notified_at)

    def fresh_batch(self):
        from uploads.models import UploadBatch

        return UploadBatch.objects.get(pk=self.batch.pk)


class TheGroupingInputDoesNotChangeTests(Base):
    def test_the_identity_analysis_reads_the_standardised_original_never_an_enhanced_file(self):
        """So an enhancement can never change who a face is grouped with: only the time at which the analysis starts moved."""
        from identities.services import _read_image

        image = self.photo()
        original = cv2.imdecode(np.frombuffer(image.preprocessing_result.processed_file.read(), np.uint8), cv2.IMREAD_COLOR)
        result = ImageEnhancementResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, requested_level="auto", requested_style="natural", status=EnhancementStatus.COMPLETED,
            trigger=EnhancementTrigger.AUTO,
        )
        different = cv2.imencode(".png", 255 - original)[1].tobytes()                      # an "enhanced" picture that is a different picture
        result.enhanced_file.save("enhanced.png", ContentFile(different))
        self.assertFalse(np.array_equal(_read_image(image.preprocessing_result), 255 - original))
        np.testing.assert_array_equal(_read_image(image.preprocessing_result), original)
        self.assertEqual(picture(1)[:4], b"\x89PNG")

"""Automatic enhancement after upload: a photograph that passes the checks is enhanced without anyone asking, safely, exactly once.

The photographs go through the real path: the quality task decides (``analyze_image_quality_task``), the enhancement it queues runs (Celery eager, or held back
with ``execute=False`` to look at a queue) and writes a real result with the real pipeline on a real stored picture, so what is checked is what the product does.
The quality *verdict* is forced (the measurements are not what is under test); the rendering policy of the tests is ``lightweight_v1`` (OpenCV only, no model
needed) except where the adaptive engine is the point.
"""
import io
import tempfile
import zipfile
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from downloads.archives import build_package_archive
from downloads.models import DownloadPackage
from downloads.services import PackageNotReadyError, ensure_package_ready
from moderation.models import ModeratorReview
from moderation.tests import ModerationFixtureMixin
from notifications.models import NotificationPreference, UserNotification
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from quality_control.services import enqueue_quality_analysis
from quality_control.tasks import analyze_image_quality_task
from uploads.models import BatchImageProgress, ImagePreprocessingResult, ImageUpload, UploadBatch
from uploads.progress import batch_progress_payload, sync_image_progress
from uploads.services import preprocess_image
from validation.models import FaceValidationResult

from . import policy
from .auto import (
    enhance_after_verdict, enhancement_state, notify_batch_if_finished, parse_auto_enhance, release_waiting, resolve_auto_enhance, verdict_allows_enhancement,
)
from .models import AutoEnhancement, EnhancementTrigger, ImageEnhancementResult
from .tasks import process_enhancement_image, release_waiting_auto

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-auto-enhance-")


def picture(seed=1, size=(480, 360), level=130):
    """A well-lit, sharp, smooth picture with a lighter oval, as PNG bytes: nothing for the adaptive engine to fix."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size[0], 0:size[1]].astype(np.float32)
    base = level + 35 * np.sin(x / 41.0 + seed) * np.cos(y / 57.0)
    frame = np.stack([base - 10, base, base + 12], axis=-1)
    cv2.ellipse(frame, (size[1] // 2, size[0] // 2), (size[1] // 5, size[0] // 4), 0, 0, 360, (level + 55, level + 60, level + 70), -1)
    frame = np.clip(frame + rng.normal(0, 1.0, frame.shape), 0, 255).astype(np.uint8)
    return cv2.imencode(".png", frame)[1].tobytes()


@override_settings(
    MEDIA_ROOT=MEDIA, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False,
    AUTO_ENHANCEMENT_ENABLED=True, AUTO_ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RENDER_POLICY="lightweight_v1",
    MAX_ACTIVE_ENHANCEMENTS_PER_USER=10, IDENTITY_AUTO_ANALYZE=False, BIOMETRIC_CONSENT_REQUIRED=False,
)
class Base(ModerationFixtureMixin, TestCase):
    """An owner, a batch that asked for automatic enhancement, and photographs that have their stored picture, face check and quality verdict."""

    def setUp(self):
        self.owner = self.make_user("auto-owner")
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple", auto_enhance=True)
        self.count = 0

    def photo(self, batch=None, status="uploaded", face_decision="accepted", seed=None):
        self.count += 1
        batch = batch or self.batch
        owner = batch.owner
        encoded = picture(seed or self.count)
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=f"auto-{self.count}.png", file_extension=".png", mime_type="image/png", file_size=len(encoded),
            width=360, height=480, status=status,
        )
        image.original_file.save(f"auto-{self.count}.png", ContentFile(encoded))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=owner, image=image, status="completed", output_format="png", output_name=f"auto-{self.count}.png", output_width=360, output_height=480,
            processed_file_size=len(encoded),
        )
        preprocessing.processed_file.save(f"auto-{self.count}.png", ContentFile(encoded))
        FaceValidationResult.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, decision=face_decision, face_count=1,
            faces=[{"x": 100, "y": 120, "width": 160, "height": 200, "confidence": 0.9, "area_ratio": 0.18}],
        )
        return image

    def decide(self, image, decision="accepted", execute=True):
        """Give ``image`` its quality verdict through the real quality task; the enhancement it queues runs after the commit when ``execute``."""
        quality, _ = ImageQualityDecision.objects.update_or_create(
            image=image,
            defaults={"owner": image.owner, "preprocessing_result": image.preprocessing_result, "face_validation": getattr(image, "face_validation", None),
                      "status": "queued", "decision": "", "task_id": "", "rejection_reasons": []},
        )
        with ExitStack() as stack:
            stack.enter_context(patch("quality_control.services._read_standardized", return_value=np.zeros((10, 10, 3), np.uint8)))
            stack.enter_context(patch("quality_control.services.analyze_image_quality", return_value={"defects": []}))
            stack.enter_context(patch("quality_control.services.with_face_sharpness", side_effect=lambda metrics, image, validation: metrics))
            stack.enter_context(patch("quality_control.services.calculate_scores", return_value={}))
            stack.enter_context(patch("quality_control.services.make_decision", return_value=(decision, ["face_too_small"] if decision == "rejected" else [], "test")))
            with self.captureOnCommitCallbacks(execute=execute):
                analyze_image_quality_task.apply(args=[quality.pk])
        return ImageQualityDecision.objects.get(pk=quality.pk)

    def results(self, image):
        return ImageEnhancementResult.objects.filter(image=image)

    def run_queued(self, result):
        """Run a queued result the way its task would, with the task id it was queued under (another id would be a superseded duplicate)."""
        with self.captureOnCommitCallbacks(execute=True):
            process_enhancement_image.apply(args=[result.pk], task_id=result.task_id)

    def fresh(self, image):
        return ImageUpload.objects.get(pk=image.pk)

    def organization_batch(self):
        moderator = self.make_user("auto-moderator")
        organization = create_organization(name="Auto Co", owner=self.make_user("auto-org-owner"))
        OrganizationMembership.objects.create(organization=organization, user=moderator, role="moderator", status="active")
        OrganizationMembership.objects.create(organization=organization, user=self.owner, role="member", status="active")
        self.batch.organization = organization
        self.batch.save(update_fields=["organization"])
        return organization, moderator

    def approve(self, organization, moderator, image):
        review = ModeratorReview.objects.get(image=image, source="quality")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.authenticated_client(moderator).post(
                reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": review.pk}), {"action": "approve"}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class ExactlyOneEnhancementTests(Base):
    def test_an_accepted_photo_gets_exactly_one_automatic_enhancement(self):
        image = self.photo()
        self.decide(image, "accepted")
        (result,) = self.results(image)
        self.assertEqual((result.trigger, result.status, result.requested_level, result.requested_style), (EnhancementTrigger.AUTO, "completed", "auto", "natural"))
        record = AutoEnhancement.objects.get(image=image)
        self.assertEqual((record.state, record.result_id, record.skip_reason), ("started", result.pk, ""))
        self.assertTrue(result.enhanced_file)

    def test_a_rejected_photo_is_never_enhanced_whatever_rejected_it(self):
        by_quality = self.photo()
        self.decide(by_quality, "rejected")
        by_face_check = self.photo(face_decision="rejected")
        self.decide(by_face_check, "accepted")
        at_upload = self.photo(status="rejected")
        enhance_after_verdict(at_upload, verdict="accepted")
        for image in (by_quality, by_face_check, at_upload):
            with self.subTest(image=image.original_name):
                self.assertFalse(self.results(image).exists())
                self.assertFalse(AutoEnhancement.objects.filter(image=image).exists())

    def test_calling_it_again_never_makes_a_second_one(self):
        image = self.photo()
        self.decide(image, "accepted")
        for _ in range(3):
            self.assertIsNone(enhance_after_verdict(self.fresh(image), verdict="accepted"))
        self.assertEqual(self.results(image).count(), 1)
        self.assertEqual(AutoEnhancement.objects.filter(image=image).count(), 1)

    def test_a_retry_and_a_new_analysis_of_a_decided_photo_still_leave_one(self):
        image = self.photo()
        self.decide(image, "accepted")
        with self.captureOnCommitCallbacks(execute=True):
            preprocess_image(self.fresh(image), requested_format="auto")        # the repeat of an already-processed photograph (a retry)
        self.decide(image, "accepted")                                           # the quality analysis run again
        with patch("uploads.services.preprocess_image", wraps=preprocess_image):
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_quality_analysis(self.fresh(image))
        self.assertEqual(self.results(image).count(), 1)
        self.assertEqual(AutoEnhancement.objects.filter(image=image).count(), 1)

    def test_a_photo_a_person_already_enhanced_is_left_alone(self):
        image = self.photo()
        from .services import create_enhancement_result

        create_enhancement_result(image, "auto").save()
        self.decide(image, "accepted")
        self.assertEqual(self.results(image).count(), 1)
        self.assertEqual(self.results(image).get().trigger, EnhancementTrigger.MANUAL)
        self.assertFalse(AutoEnhancement.objects.filter(image=image).exists())

    def test_a_photo_nobody_decided_yet_waits_for_its_verdict(self):
        image = self.photo()
        self.assertEqual(verdict_allows_enhancement(image), (False, "quality_verdict"))
        self.assertIsNone(enhance_after_verdict(image))
        self.assertFalse(self.results(image).exists())


class NeedsReviewTests(Base):
    def test_in_a_personal_workspace_a_review_verdict_is_final_so_it_is_enhanced_at_once(self):
        image = self.photo()
        self.decide(image, "needs_review")
        self.assertEqual(self.results(image).count(), 1)

    def test_in_an_organization_it_waits_for_a_moderator_and_runs_once_after_approval(self):
        organization, moderator = self.organization_batch()
        image = self.photo()
        self.decide(image, "needs_review")
        self.assertFalse(self.results(image).exists())
        self.assertEqual(verdict_allows_enhancement(self.fresh(image)), (False, "moderator_approval"))

        self.approve(organization, moderator, image)

        self.assertEqual(self.results(image).count(), 1)
        self.assertEqual(self.results(image).get().trigger, EnhancementTrigger.AUTO)

    def test_a_moderator_who_rejects_it_or_keeps_it_in_review_starts_nothing(self):
        organization, moderator = self.organization_batch()
        rejected, held = self.photo(), self.photo()
        for image, action in ((rejected, "reject"), (held, "keep_in_review")):
            self.decide(image, "needs_review")
            review = ModeratorReview.objects.get(image=image, source="quality")
            with self.captureOnCommitCallbacks(execute=True):
                self.authenticated_client(moderator).post(
                    reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": review.pk}), {"action": action}, format="json",
                )
        self.assertFalse(ImageEnhancementResult.objects.exists())


class SwitchesTests(Base):
    def test_a_batch_that_did_not_ask_for_it_is_not_enhanced(self):
        self.batch.auto_enhance = False
        self.batch.save(update_fields=["auto_enhance"])
        image = self.photo()
        self.decide(image, "accepted")
        self.assertFalse(self.results(image).exists())
        self.assertEqual(enhancement_state(self.fresh(image))["status"], "not_applicable")

    def test_the_operators_switch_stops_every_automatic_enhancement(self):
        image = self.photo()
        with override_settings(AUTO_ENHANCEMENT_ENABLED=False):
            self.decide(image, "accepted")
            self.assertFalse(resolve_auto_enhance(self.owner, True))
        self.assertFalse(self.results(image).exists())

    def test_the_account_setting_is_the_default_and_an_upload_overrides_it(self):
        self.assertTrue(resolve_auto_enhance(self.owner))                         # on unless the person turned it off
        NotificationPreference.objects.update_or_create(user=self.owner, defaults={"auto_enhance": False})
        self.assertFalse(resolve_auto_enhance(self.owner))
        self.assertTrue(resolve_auto_enhance(self.owner, True))
        NotificationPreference.objects.filter(user=self.owner).update(auto_enhance=True)
        self.assertFalse(resolve_auto_enhance(self.owner, False))

    def test_the_form_field_accepts_true_false_and_nothing_else(self):
        for value, expected in ((None, None), ("", None), ("true", True), ("False", False), ("1", True), ("0", False), ("on", True), ("off", False), (True, True)):
            with self.subTest(value=value):
                self.assertIs(parse_auto_enhance(value), expected)
        with self.assertRaises(ValueError):
            parse_auto_enhance("maybe")

    def test_a_cancelled_batch_is_not_enhanced(self):
        image = self.photo()
        self.batch.cancellation_requested = True
        self.batch.save(update_fields=["cancellation_requested"])
        self.decide(image, "accepted")
        self.assertFalse(self.results(image).exists())


@override_settings(ADAPTIVE_ALWAYS_ENHANCE=False)
class PolicyTests(Base):
    @override_settings(AUTO_ENHANCEMENT_RENDER_POLICY="adaptive_v1", ENHANCEMENT_RENDER_POLICY="lightweight_v1")
    def test_an_automatic_enhancement_runs_under_its_own_policy_and_a_persons_request_keeps_theirs(self):
        image = self.photo()
        self.decide(image, "accepted")
        automatic = self.results(image).get()
        self.assertEqual(automatic.applied_parameters["render_policy"]["name"], "adaptive_v1")
        self.assertIn(automatic.applied_parameters["adaptive"]["outcome"], ("enhanced", "partially_enhanced", "original_preserved"))

        other = self.photo()
        self.decide(other, "accepted", execute=False)
        AutoEnhancement.objects.filter(image=other).delete()
        ImageEnhancementResult.objects.filter(image=other).delete()
        client = self.authenticated_client(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(reverse("enhancement:image-enhance", kwargs={"image_id": other.pk}), {"level": "auto", "style": "natural"}, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        manual = self.results(other).get()
        self.assertEqual((manual.trigger, manual.applied_parameters["render_policy"]["name"]), ("manual", "lightweight_v1"))
        self.assertEqual(policy.active_policy(), "lightweight_v1")                 # nothing leaks out of the automatic run

    def test_the_policy_override_covers_only_the_code_it_wraps(self):
        self.assertEqual(policy.active_policy(), "lightweight_v1")
        with policy.use_policy("adaptive_v1"):
            self.assertEqual(policy.active_policy(), "adaptive_v1")
            with policy.use_policy("balanced_v1"):
                self.assertEqual(policy.active_policy(), "balanced_v1")
            self.assertEqual(policy.active_policy(), "adaptive_v1")
        self.assertEqual(policy.active_policy(), "lightweight_v1")
        with self.assertRaises(ValueError):
            with policy.use_policy("nonsense"):
                pass

    @override_settings(AUTO_ENHANCEMENT_RENDER_POLICY="adaptive_v1")
    def test_a_clean_photograph_ends_as_original_preserved_which_is_a_success_and_the_batch_completes(self):
        image = self.photo()
        self.decide(image, "accepted")
        result = self.results(image).get()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.applied_parameters["adaptive"]["outcome"], "original_preserved")
        state = enhancement_state(self.fresh(image))
        self.assertEqual((state["status"], state["outcome"]), ("completed", "original_preserved"))
        self.assertTrue(state["summary"])
        payload = batch_progress_payload(self.batch)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["enhancement_finished"])


class CapacityTests(Base):
    def queued(self):
        return ImageEnhancementResult.objects.filter(status__in=("pending", "queued", "processing"))

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=2)
    def test_over_the_limit_the_rest_wait_and_start_as_slots_free_up_none_fail(self):
        images = [self.photo() for _ in range(5)]
        for image in images:
            self.decide(image, "accepted", execute=False)                         # decided, but the queued enhancements have not run
        self.assertEqual(self.queued().count(), 2)
        self.assertEqual(AutoEnhancement.objects.filter(state="waiting").count(), 3)
        payload = batch_progress_payload(self.batch)
        waiting = [row["enhancement"] for row in payload["images"] if row["enhancement"]["waiting_for"] == "capacity"]
        self.assertEqual(len(waiting), 3)
        self.assertFalse(payload["enhancement_finished"])
        self.assertNotIn(payload["status"], ("completed", "partial", "failed"))

        for _ in range(10):
            queued = self.queued().first()
            if queued is None:
                break
            self.run_queued(queued)
        self.assertFalse(self.queued().exists())

        self.assertEqual(ImageEnhancementResult.objects.filter(trigger="auto", status="completed").count(), 5)
        self.assertFalse(AutoEnhancement.objects.filter(state="waiting").exists())
        self.assertTrue(batch_progress_payload(self.batch)["enhancement_finished"])
        self.assertEqual(batch_progress_payload(self.batch)["status"], "completed")

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1, AUTO_ENHANCEMENT_CAPACITY_WAIT_MINUTES=30)
    def test_a_photo_that_waits_too_long_is_skipped_for_capacity_and_the_upload_is_untouched(self):
        first, second = self.photo(), self.photo()
        self.decide(first, "accepted", execute=False)
        self.decide(second, "accepted", execute=False)
        AutoEnhancement.objects.filter(image=second).update(waiting_since=timezone.now() - timedelta(minutes=31))

        release_waiting()

        record = AutoEnhancement.objects.get(image=second)
        self.assertEqual((record.state, record.skip_reason), ("skipped", "capacity"))
        state = enhancement_state(self.fresh(second))
        self.assertEqual((state["status"], state["skipped_reason"]), ("skipped", "capacity"))
        self.assertEqual(self.fresh(second).status, "uploaded")                    # never failed, never hidden
        self.assertTrue(self.fresh(second).original_file)

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1)
    def test_the_scheduler_starts_waiting_photos_when_a_slot_is_free(self):
        first, second = self.photo(), self.photo()
        self.decide(first, "accepted", execute=False)
        self.decide(second, "accepted", execute=False)
        self.assertEqual(AutoEnhancement.objects.get(image=second).state, "waiting")
        ImageEnhancementResult.objects.filter(image=first).update(status="completed")      # the slot is free, nothing told the owner's other photo yet

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(release_waiting_auto.apply().get(), {"started": 1})

        self.assertEqual(AutoEnhancement.objects.get(image=second).state, "started")
        self.assertEqual(self.results(second).get().status, "completed")

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1)
    def test_one_owner_at_the_limit_does_not_hold_up_another_owner(self):
        other = self.make_user("auto-other-owner")
        other_batch = UploadBatch.objects.create(owner=other, upload_type="multiple", auto_enhance=True)
        mine_running, mine_waiting = self.photo(), self.photo()
        theirs_running, theirs_waiting = self.photo(batch=other_batch), self.photo(batch=other_batch)
        for image in (mine_running, mine_waiting, theirs_running, theirs_waiting):
            self.decide(image, "accepted", execute=False)
        self.assertEqual([AutoEnhancement.objects.get(image=image).state for image in (mine_running, mine_waiting, theirs_running, theirs_waiting)],
                         ["started", "waiting", "started", "waiting"])
        ImageEnhancementResult.objects.filter(image=theirs_running).update(status="completed")        # only the other owner's slot is free

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(release_waiting(), 1)

        self.assertEqual(AutoEnhancement.objects.get(image=mine_waiting).state, "waiting")             # still at the limit: passed over, not waited for
        self.assertEqual(AutoEnhancement.objects.get(image=theirs_waiting).state, "started")


class FailureTests(Base):
    def test_a_failed_enhancement_never_hides_the_upload_and_the_existing_retry_works(self):
        image = self.photo()
        with patch("enhancement.tasks.process_enhancement_result", side_effect=RuntimeError("model fell over")):
            self.decide(image, "accepted")
        result = self.results(image).get()
        self.assertEqual(result.status, "failed")
        image = self.fresh(image)
        self.assertEqual(image.status, "uploaded")
        self.assertTrue(image.original_file)
        state = enhancement_state(image)
        self.assertEqual((state["status"], state["outcome"]), ("failed", "failed"))
        self.assertIn("original photograph is unchanged", state["summary"])
        sync_image_progress(image.pk)
        progress = BatchImageProgress.objects.get(image=image)
        self.assertEqual((progress.status, progress.failure_stage), ("failed", "enhancement"))

        from uploads.services import retry_batch

        with self.captureOnCommitCallbacks(execute=True):
            retry_batch(UploadBatch.objects.get(pk=self.batch.pk))
        result.refresh_from_db()
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.results(image).count(), 1)

    def test_a_queue_that_is_down_fails_the_result_not_the_upload(self):
        image = self.photo()
        with patch("enhancement.tasks.process_enhancement_image.apply_async", side_effect=RuntimeError("broker down")):
            self.decide(image, "accepted")
        result = self.results(image).get()
        self.assertEqual((result.status, result.failure_reason), ("failed", "The background enhancement queue is unavailable."))
        self.assertEqual(ImageQualityDecision.objects.get(image=image).decision, "accepted")       # the verdict stands
        self.assertEqual(self.fresh(image).status, "uploaded")

    def test_something_unexpected_while_starting_never_undoes_the_verdict(self):
        image = self.photo()
        with patch("enhancement.auto._release", side_effect=RuntimeError("unexpected")):
            quality = self.decide(image, "accepted")
        self.assertEqual((quality.status, quality.decision), ("completed", "accepted"))
        record = AutoEnhancement.objects.get(image=image)
        self.assertEqual((record.state, record.skip_reason), ("skipped", "error"))
        self.assertFalse(self.results(image).exists())
        self.assertEqual(enhancement_state(self.fresh(image))["status"], "skipped")

    def test_a_photo_that_cannot_be_enhanced_is_skipped_not_failed(self):
        image = self.photo()
        ImagePreprocessingResult.objects.filter(image=image).update(status="failed")
        self.decide(image, "accepted")
        record = AutoEnhancement.objects.get(image=image)
        self.assertEqual((record.state, record.skip_reason), ("skipped", "not_possible"))


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class ProgressTests(Base):
    def test_a_photo_waiting_for_its_verdict_shows_the_enhancement_queued_and_holds_the_batch(self):
        image = self.photo()
        payload = batch_progress_payload(self.batch)
        (row,) = payload["images"]
        self.assertEqual((row["enhancement"]["status"], row["enhancement"]["waiting_for"]), ("queued", "quality_verdict"))
        self.assertTrue(payload["auto_enhance"])
        self.assertFalse(payload["enhancement_finished"])
        del image

    def test_each_photo_reports_its_stage_and_the_batch_completes_only_when_they_are_done(self):
        accepted, rejected = self.photo(), self.photo()
        self.decide(accepted, "accepted", execute=False)                        # enhancement queued, not run
        self.decide(rejected, "rejected")
        mid = {row["image_id"]: row for row in batch_progress_payload(self.batch)["images"]}
        self.assertEqual(mid[accepted.pk]["enhancement"]["status"], "queued")
        self.assertEqual(mid[accepted.pk]["stage"], "enhancement")
        self.assertEqual(mid[rejected.pk]["enhancement"]["status"], "not_applicable")
        self.assertNotIn(batch_progress_payload(self.batch)["status"], ("completed", "partial"))

        self.run_queued(self.results(accepted).get())

        done = batch_progress_payload(self.batch)
        rows = {row["image_id"]: row for row in done["images"]}
        self.assertEqual(rows[accepted.pk]["enhancement"]["status"], "completed")
        self.assertEqual(rows[accepted.pk]["status"], "completed")
        self.assertEqual(done["status"], "partial")                              # a rejected photo makes a batch partial, as before
        self.assertTrue(done["enhancement_finished"])

    def test_a_photo_waiting_for_a_moderator_does_not_hold_the_batch_back(self):
        organization, moderator = self.organization_batch()
        image = self.photo()
        self.decide(image, "needs_review")
        payload = batch_progress_payload(self.batch)
        (row,) = payload["images"]
        self.assertEqual((row["enhancement"]["status"], row["enhancement"]["waiting_for"]), ("queued", "moderator_approval"))
        self.assertTrue(payload["enhancement_finished"])
        self.assertEqual(payload["status"], "partial")                           # needs_review, as before
        self.approve(organization, moderator, image)
        self.assertEqual(enhancement_state(self.fresh(image))["status"], "completed")

    def test_every_field_that_was_in_the_payload_is_still_there(self):
        image = self.photo()
        self.decide(image, "accepted")
        payload = batch_progress_payload(self.batch)
        for key in ("batch_id", "upload_type", "status", "stage", "progress_percentage", "cancellation_requested", "retry_count", "counts", "estimated_completion_at",
                    "identity_grouping_finished", "suggested_merge_count", "images", "updated_at"):
            self.assertIn(key, payload)
        for key in ("image_id", "filename", "stage", "status", "display_status", "progress_percentage", "failure_stage", "failure_reason", "retry_count",
                    "updated_at", "identity_grouping"):
            self.assertIn(key, payload["images"][0])
        self.assertEqual(set(payload["images"][0]["enhancement"]), {"status", "outcome", "summary", "waiting_for", "skipped_reason"})


@override_settings(QUALITY_CHECKS_REJECT=True, ADAPTIVE_ALWAYS_ENHANCE=False)      # about refusing a photograph and about photographs the engine leaves as they were: the defaults no longer do either (test_never_reject.py, test_adaptive_always_enhance.py)
class NotificationTests(Base):
    def events(self):
        return UserNotification.objects.filter(recipient=self.owner, event_type="batch_enhanced")

    def test_one_notification_per_batch_none_per_photo(self):
        photos = [self.photo() for _ in range(3)]
        for image in photos:
            self.decide(image, "accepted")
        (notification,) = self.events()
        self.assertEqual(notification.message, "3 photos enhanced.")
        self.assertEqual(notification.metadata["enhanced"], 3)
        per_photo = UserNotification.objects.filter(recipient=self.owner, title__in=("Photo enhanced", "Enhancement failed"))
        self.assertFalse(per_photo.exists())

    def test_it_is_sent_when_the_last_photo_finishes_not_before(self):
        first, second = self.photo(), self.photo()
        self.decide(first, "accepted")                                           # enhanced, but the second photo has no verdict yet
        self.assertEqual(self.events().count(), 0)
        self.decide(second, "accepted", execute=False)
        self.assertEqual(self.events().count(), 0)                               # its enhancement is queued: not finished
        self.run_queued(self.results(second).get())
        self.assertEqual(self.events().count(), 1)

    def test_a_rejected_last_photo_does_not_leave_the_batch_unannounced(self):
        accepted, rejected = self.photo(), self.photo()
        self.decide(accepted, "accepted", execute=False)
        self.decide(rejected, "rejected")
        self.assertEqual(self.events().count(), 0)
        self.run_queued(self.results(accepted).get())
        (notification,) = self.events()
        self.assertEqual(notification.message, "1 photo enhanced.")

    @override_settings(AUTO_ENHANCEMENT_RENDER_POLICY="adaptive_v1")
    def test_photos_the_engine_kept_as_they_were_are_counted_as_kept_not_enhanced(self):
        image = self.photo()
        self.decide(image, "accepted")
        (notification,) = self.events()
        self.assertTrue(notification.message.startswith("0 photos enhanced (1 kept as uploaded"), notification.message)

    def test_a_second_check_never_sends_a_second_one(self):
        self.decide(self.photo(), "accepted")
        self.assertIsNone(notify_batch_if_finished(self.batch.pk))
        self.assertEqual(self.events().count(), 1)

    def test_the_preference_for_batch_results_switches_it(self):
        NotificationPreference.objects.update_or_create(user=self.owner, defaults={"batch_upload_finished": False})
        self.decide(self.photo(), "accepted")
        self.assertEqual(self.events().count(), 0)

    def test_the_scheduler_announces_a_batch_whose_last_enhancement_ended_quietly(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)
        ImageEnhancementResult.objects.filter(image=image).update(status="failed", finished_at=timezone.now())      # closed by the reaper, nobody told
        self.assertEqual(self.events().count(), 0)
        release_waiting()
        (notification,) = self.events()
        self.assertEqual(notification.severity, "warning")
        self.assertIn("1 could not be enhanced", notification.message)


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class PackageTests(Base):
    def package(self, kind="accepted_results"):
        return DownloadPackage.objects.create(owner=self.owner, batch=self.batch, package_type=kind, report_format="json", status="queued", task_id="t")

    def test_the_accepted_results_zip_holds_the_enhanced_photo_where_there_is_one_and_the_original_for_the_rest_and_says_which(self):
        enhanced, skipped, rejected = self.photo(), self.photo(), self.photo()
        self.decide(enhanced, "accepted")
        self.decide(skipped, "accepted", execute=False)
        ImageEnhancementResult.objects.filter(image=skipped).delete()
        AutoEnhancement.objects.filter(image=skipped).update(state="skipped", skip_reason="capacity", result=None)
        self.decide(rejected, "rejected")
        self.batch.processing_status = "completed"
        self.batch.save(update_fields=["processing_status"])
        ensure_package_ready(self.batch, "accepted_results")

        path, manifest, _size, _checksum = build_package_archive(self.package())

        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        path.unlink()
        by_image = {item["image_id"]: item for item in manifest["files"]}
        self.assertEqual(set(by_image), {enhanced.pk, skipped.pk})
        self.assertEqual((by_image[enhanced.pk]["version"], by_image[enhanced.pk]["resource_type"]), ("enhanced", "enhanced"))
        self.assertTrue(by_image[enhanced.pk]["archive_path"].startswith("enhanced/"))
        self.assertEqual((by_image[skipped.pk]["version"], by_image[skipped.pk]["resource_type"]), ("original", "original"))
        self.assertTrue(by_image[skipped.pk]["archive_path"].startswith("original/"))
        self.assertIn("original is included instead", by_image[skipped.pk]["reason"])
        self.assertEqual((manifest["summary"]["enhanced_files"], manifest["summary"]["original_files"]), (1, 1))
        self.assertEqual(len([name for name in names if name.startswith(("enhanced/", "original/"))]), 2)

    def test_a_batch_that_asked_for_automatic_enhancement_can_be_packaged_even_if_none_finished(self):
        image = self.photo()
        self.decide(image, "accepted", execute=False)
        ImageEnhancementResult.objects.filter(image=image).update(status="failed")
        self.batch.processing_status = "completed"
        self.batch.save(update_fields=["processing_status"])
        ensure_package_ready(self.batch, "accepted_results")                    # the originals stand in

    def test_a_batch_nobody_enhanced_and_that_did_not_ask_still_answers_enhancement_required(self):
        self.batch.auto_enhance = False
        self.batch.processing_status = "completed"
        self.batch.save(update_fields=["auto_enhance", "processing_status"])
        image = self.photo()
        self.decide(image, "accepted")
        with self.assertRaises(PackageNotReadyError) as raised:
            ensure_package_ready(self.batch, "accepted_results")
        self.assertEqual(raised.exception.code, "enhancement_required")


@override_settings(
    MEDIA_ROOT=MEDIA, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False,
    AUTO_ENHANCEMENT_ENABLED=True, AUTO_ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RENDER_POLICY="lightweight_v1",
    MAX_ACTIVE_ENHANCEMENTS_PER_USER=10, IDENTITY_AUTO_ANALYZE=False, BIOMETRIC_CONSENT_REQUIRED=False,
)
class UploadAnswerTests(ModerationFixtureMixin, TransactionTestCase):
    """A real upload through the API: the checks run for real, the face check is stood in for, and the verdict is forced to ``accepted``.

    A transaction test, so ``on_commit`` runs when the request commits, exactly as it does for a person: the answer already holds the results in inline mode.
    """

    def setUp(self):
        self.owner = self.make_user("upload-owner")

    def upload(self, client, files, data=None, endpoint="multiple"):
        files = [self.png(name, seed) for name, seed in files]
        field = "files" if endpoint == "multiple" else "file"
        body = {field: files if endpoint == "multiple" else files[0], **(data or {})}
        with ExitStack() as stack:
            stack.enter_context(patch("validation.services._detect", return_value=(np.array([[80, 60, 200, 220]]), [0.93])))
            stack.enter_context(patch("quality_control.services.make_decision", return_value=("accepted", [], "ok")))
            return client.post(f"/api/images/upload/{endpoint}/", body, format="multipart")

    @staticmethod
    def png(name, seed):
        buffer = io.BytesIO(picture(seed, size=(600, 800)))
        buffer.name = name
        return buffer

    def test_inline_mode_has_the_results_already_in_the_answer(self):
        client = self.authenticated_client(self.owner)
        response = self.upload(client, [("a.png", 1), ("b.png", 2)])
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["processing_mode"], "inline")
        self.assertTrue(body["auto_enhance"])
        self.assertEqual(len(body["images"]), 2)
        for image in body["images"]:
            self.assertEqual(image["enhancement"]["status"], "completed", image)
        batch = UploadBatch.objects.get(pk=body["id"])
        self.assertEqual(ImageEnhancementResult.objects.filter(image__batch=batch, trigger="auto").count(), 2)
        self.assertEqual(batch_progress_payload(batch)["status"], "completed")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_with_a_worker_the_answer_says_the_enhancement_is_queued_behind_the_checks(self):
        client = self.authenticated_client(self.owner)
        with patch("uploads.views.enqueue_batch_preprocessing", side_effect=lambda batch: batch):         # no broker in a test
            response = self.upload(client, [("a.png", 1), ("b.png", 2)])
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["processing_mode"], body["auto_enhance"]), ("background", True))
        for image in body["images"]:
            self.assertEqual(image["enhancement"]["status"], "queued", image)
            self.assertEqual(image["enhancement"]["waiting_for"], "quality_verdict")

    def test_an_upload_can_turn_it_off_and_the_account_setting_is_the_default(self):
        client = self.authenticated_client(self.owner)
        off = self.upload(client, [("a.png", 1), ("b.png", 2)], {"auto_enhance": "false"})
        self.assertEqual(off.status_code, 201, off.content)
        self.assertFalse(off.json()["auto_enhance"])
        self.assertFalse(ImageEnhancementResult.objects.exists())
        self.assertEqual({row["enhancement"]["status"] for row in off.json()["images"]}, {"not_applicable"})

        NotificationPreference.objects.update_or_create(user=self.owner, defaults={"auto_enhance": False})
        by_account = self.upload(client, [("c.png", 3), ("d.png", 4)])
        self.assertFalse(by_account.json()["auto_enhance"])
        forced_on = self.upload(client, [("e.png", 5), ("f.png", 6)], {"auto_enhance": "true"})
        self.assertTrue(forced_on.json()["auto_enhance"])
        self.assertEqual(ImageEnhancementResult.objects.filter(image__batch_id=forced_on.json()["id"]).count(), 2)

    def test_the_single_upload_takes_the_field_too_and_a_bad_value_is_a_400(self):
        client = self.authenticated_client(self.owner)
        single = self.upload(client, [("s.png", 7)], {"auto_enhance": "true"}, endpoint="single")
        self.assertEqual(single.status_code, 201, single.content)
        self.assertTrue(single.json()["auto_enhance"])
        bad = client.post("/api/images/upload/single/", {"file": self.png("bad.png", 8), "auto_enhance": "maybe"}, format="multipart")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("auto_enhance", bad.json())

    def test_the_zip_upload_takes_the_field(self):
        client = self.authenticated_client(self.owner)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("a.png", picture(1, size=(600, 800)))
            bundle.writestr("b.png", picture(2, size=(600, 800)))
        archive.seek(0)
        archive.name = "photos.zip"
        with ExitStack() as stack:
            stack.enter_context(patch("validation.services._detect", return_value=(np.array([[80, 60, 200, 220]]), [0.93])))
            stack.enter_context(patch("quality_control.services.make_decision", return_value=("accepted", [], "ok")))
            response = client.post("/api/images/upload/zip/", {"file": archive, "auto_enhance": "false"}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(response.json()["auto_enhance"])
        self.assertFalse(ImageEnhancementResult.objects.exists())

    def test_the_progress_endpoint_carries_the_stage(self):
        client = self.authenticated_client(self.owner)
        body = self.upload(client, [("a.png", 1), ("b.png", 2)]).json()
        progress = client.get(reverse("upload-batch-progress", kwargs={"batch_id": body["id"]})).json()
        self.assertTrue(progress["enhancement_finished"])
        self.assertEqual([row["enhancement"]["status"] for row in progress["images"]], ["completed", "completed"])


class PreferencesAndGuardTests(Base):
    def test_the_account_setting_is_on_by_default_and_changed_through_the_preferences_endpoint(self):
        client = self.authenticated_client(self.owner)
        self.assertIs(client.get(reverse("notification-preferences")).json()["auto_enhance"], True)
        off = client.patch(reverse("notification-preferences"), {"auto_enhance": False}, format="json")
        self.assertEqual(off.status_code, 200)
        self.assertIs(off.json()["auto_enhance"], False)
        self.assertIs(client.get(reverse("notification-preferences")).json()["auto_enhance"], False)
        self.assertFalse(NotificationPreference.objects.get(user=self.owner).auto_enhance)

    def test_every_preference_that_was_there_is_still_there(self):
        body = self.authenticated_client(self.owner).get(reverse("notification-preferences")).json()
        for key in ("photo_accepted", "photo_rejected", "batch_upload_finished", "person_group_created", "email_notifications", "live_session_alerts",
                    "moderator_review_updates", "updated_at"):
            self.assertIn(key, body)


class ProductionGuardTests(SimpleTestCase):
    def problems(self, **settings_):
        from config.production_guard import model_rule

        base = {"ADAPTIVE_DEBUG_ARTIFACTS": False, "ZERO_DCE_ENABLED": True, "ENHANCEMENT_RENDER_POLICY": "lightweight_v1",
                "AUTO_ENHANCEMENT_ENABLED": True, "AUTO_ENHANCEMENT_RENDER_POLICY": "adaptive_v1"}
        return model_rule({**base, **settings_})

    def test_zero_dce_under_the_automatic_policy_is_refused_in_production_unless_acknowledged(self):
        (problem,) = self.problems()
        self.assertIn("Zero-DCE", problem)
        self.assertEqual(self.problems(NONCOMMERCIAL_MODELS_ACKNOWLEDGED=True), [])

    def test_it_is_fine_when_zero_dce_is_off_or_every_policy_is_lightweight(self):
        self.assertEqual(self.problems(ZERO_DCE_ENABLED=False), [])
        self.assertEqual(self.problems(AUTO_ENHANCEMENT_RENDER_POLICY="lightweight_v1"), [])
        self.assertEqual(self.problems(AUTO_ENHANCEMENT_ENABLED=False), [])

    def test_a_persons_own_request_policy_is_still_checked(self):
        (problem,) = self.problems(ENHANCEMENT_RENDER_POLICY="learned_v1", AUTO_ENHANCEMENT_ENABLED=False)
        self.assertIn("Zero-DCE", problem)

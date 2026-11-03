"""Person folders are made after the quality verdict, not before it; the owner is told which folders look like one person; rejected photos leave nothing behind.

The photographs go through the real path: the quality task (``analyze_image_quality_task``) decides, and the identity analysis it queues runs (Celery eager) with
a three-dimensional stand-in for the face model, so what is checked is what the pipeline does, not a mock of it.
"""
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
from django.test import override_settings
from django.urls import reverse

from moderation.models import ModeratorReview
from moderation.tests import ModerationFixtureMixin
from notifications.models import NotificationPreference, UserNotification
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from quality_control.tasks import analyze_image_quality_task
from uploads.progress import batch_progress_payload, sync_image_progress

from .models import IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup, IdentityMembership
from .suggestions import announce_suggested_merges, suggested_merges
from .test_regrouping import Base, Provider, a_face, angle, vec
from .verdict import batch_grouping_finished, grouping_status, verdict_allows_grouping

State = IdentityMembership.State
MERGE_EVENT = "identity_merge_suggested"


@override_settings(IDENTITY_AUTO_ANALYZE=True, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False, MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY=0.40)
class Pipeline(ModerationFixtureMixin, Base):
    """``Base`` (owner with consent, a stand-in face model, photographs with their face check) plus a way to give a photograph its quality verdict."""

    def check(self, image, decision="accepted", execute=True):
        """Run the quality check on ``image`` through the task, with the outcome ``decision``; the identity analysis it queues runs after the commit."""
        quality, _ = ImageQualityDecision.objects.update_or_create(
            image=image,
            defaults={"owner": self.owner, "preprocessing_result": image.preprocessing_result, "face_validation": image.face_validation,
                      "status": "queued", "decision": "", "task_id": "", "rejection_reasons": []},
        )
        with ExitStack() as stack:
            stack.enter_context(patch("quality_control.services._read_standardized", return_value=np.zeros((10, 10, 3), np.uint8)))
            stack.enter_context(patch("quality_control.services.analyze_image_quality", return_value={"defects": []}))
            stack.enter_context(patch("quality_control.services.with_face_sharpness", side_effect=lambda metrics, image, validation: metrics))
            stack.enter_context(patch("quality_control.services.calculate_scores", return_value={}))
            stack.enter_context(patch("quality_control.services.make_decision", return_value=(decision, ["face_too_small"] if decision != "accepted" else [], "test")))
            stack.enter_context(patch("identities.services.get_identity_provider", return_value=self.provider))
            stack.enter_context(patch("identities.services._read_image", return_value=np.zeros((10, 10, 3), np.uint8)))
            with self.captureOnCommitCallbacks(execute=execute):
                analyze_image_quality_task.apply(args=[quality.pk])
        return ImageQualityDecision.objects.get(pk=quality.pk)

    def model_patches(self):
        """The stand-in face model, as a context manager: enter it *outside* ``captureOnCommitCallbacks`` so the queued analysis runs while it is in place."""
        stack = ExitStack()
        stack.enter_context(patch("identities.services.get_identity_provider", return_value=self.provider))
        stack.enter_context(patch("identities.services._read_image", return_value=np.zeros((10, 10, 3), np.uint8)))
        return stack

    def folders(self, owner=None):
        return IdentityGroup.objects.filter(owner=owner or self.owner, state=IdentityGroup.State.ACTIVE)

    def partition(self):
        """The final folders as a set of sets of photographs (names, so two runs can be compared)."""
        result = set()
        for folder in self.folders():
            names = {m.identity_face.image.original_name for m in folder.memberships.exclude(state__in=[State.REMOVED, State.REJECTED]).select_related("identity_face__image")}
            if names:
                result.add(frozenset(names))
        return result

    def organization_batch(self, **organization):
        moderator = self.make_user("verdict-moderator")
        organization = create_organization(name="Verdict Co", owner=self.make_user("verdict-org-owner"), **organization)
        OrganizationMembership.objects.create(organization=organization, user=moderator, role="moderator", status="active")
        OrganizationMembership.objects.create(organization=organization, user=self.owner, role="member", status="active")
        self.batch.organization = organization
        self.batch.save(update_fields=["organization"])
        return organization, moderator


class GroupAfterTheVerdictTests(Pipeline):
    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(10), 3: angle(90)}

    def test_a_rejected_photo_never_creates_an_analysis_a_face_a_membership_or_a_folder(self):
        image = self.photo([a_face(1)])
        self.check(image, "rejected")
        self.assertFalse(IdentityAnalysis.objects.filter(image=image).exists())
        self.assertFalse(IdentityFace.objects.exists())
        self.assertFalse(IdentityMembership.objects.exists())
        self.assertFalse(IdentityGroup.objects.exists())

    def test_an_accepted_photo_is_grouped_at_once(self):
        image = self.photo([a_face(1)])
        self.check(image, "accepted")
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")
        (folder,) = self.folders()
        self.assertEqual(folder.member_count, 1)

    def test_a_photo_is_not_grouped_by_preprocessing_or_before_it_has_a_verdict(self):
        image = self.photo([a_face(1)])
        self.check(image, "accepted", execute=False)                  # decided, but the queued analysis has not run yet
        analysis = IdentityAnalysis.objects.get(image=image)
        self.assertEqual(analysis.status, "queued")
        self.assertFalse(IdentityGroup.objects.exists())              # nothing is in a folder until the analysis runs

    def test_without_automatic_grouping_nothing_is_queued(self):
        image = self.photo([a_face(1)])
        with override_settings(IDENTITY_AUTO_ANALYZE=False):
            self.check(image, "accepted")
        self.assertFalse(IdentityAnalysis.objects.exists())

    def test_a_photo_whose_face_check_rejected_it_is_not_grouped_even_if_the_quality_verdict_says_accepted(self):
        image = self.photo([a_face(1)], decision="rejected")
        self.check(image, "accepted")
        self.assertFalse(IdentityAnalysis.objects.exists())

    def test_a_photo_refused_at_upload_is_not_grouped(self):
        image = self.photo([a_face(1)], status="rejected")
        self.assertEqual(verdict_allows_grouping(image, "accepted"), (False, "rejected"))

    def test_a_failed_quality_check_leaves_the_photo_ungrouped(self):
        image = self.photo([a_face(1)])
        with patch("quality_control.services.analyze_image_quality", side_effect=RuntimeError("boom")):
            quality = ImageQualityDecision.objects.create(owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status="queued")
            with self.captureOnCommitCallbacks(execute=True), patch("quality_control.services._read_standardized", return_value=np.zeros((10, 10, 3), np.uint8)):
                analyze_image_quality_task.apply(args=[quality.pk])
        self.assertNotEqual(ImageQualityDecision.objects.get(pk=quality.pk).decision, "accepted")
        self.assertFalse(IdentityAnalysis.objects.exists())

    def test_a_grouping_that_cannot_run_does_not_fail_the_verdict(self):
        image = self.photo([a_face(1)])
        with patch("identities.verdict.enqueue_identity_analysis" if False else "identities.services.enqueue_identity_analysis", side_effect=RuntimeError("no model")):
            quality = self.check(image, "accepted")
        self.assertEqual((quality.status, quality.decision), ("completed", "accepted"))
        self.assertFalse(IdentityAnalysis.objects.exists())

    def test_an_ineligible_photo_does_not_fail_the_verdict_either(self):
        from rest_framework.exceptions import ValidationError

        image = self.photo([a_face(1)])
        with patch("identities.services.enqueue_identity_analysis", side_effect=ValidationError("The image is not eligible for identity analysis.")):
            quality = self.check(image, "accepted")
        self.assertEqual((quality.status, quality.decision), ("completed", "accepted"))


class VerdictRuleTests(Pipeline):
    """The table in ``identities/verdict.py``, asked directly (the analysis itself also refuses a rejected photo, so the end-to-end tests alone would not notice a wrong rule)."""

    def rule(self, image, verdict=None):
        return verdict_allows_grouping(type(image).objects.get(pk=image.pk), verdict)

    def test_every_row_of_the_table(self):
        image = self.photo([a_face(1)])
        self.assertEqual(self.rule(image, "accepted"), (True, "accepted"))
        self.assertEqual(self.rule(image, "rejected"), (False, "rejected"))
        self.assertEqual(self.rule(image, "needs_review"), (True, "personal_workspace"))            # no organization: nobody can decide
        self.assertEqual(self.rule(image, ""), (False, "quality_verdict"))

    def test_in_an_organization_needs_review_waits_for_a_moderator(self):
        self.organization_batch()
        image = self.photo([a_face(1)])
        self.assertEqual(self.rule(image, "needs_review"), (False, "moderator_approval"))
        self.assertEqual(self.rule(image, "accepted"), (True, "accepted"))

    def test_a_personal_workspace_is_decided_by_nobody_else(self):
        personal = create_organization(name="Mine", owner=self.owner, is_personal=True)
        self.batch.organization = personal
        self.batch.save(update_fields=["organization"])
        self.assertEqual(self.rule(self.photo([a_face(1)]), "needs_review"), (True, "personal_workspace"))

    def test_without_a_given_verdict_the_saved_one_is_read(self):
        image = self.photo([a_face(1)])
        self.assertEqual(self.rule(image), (False, "quality_verdict"))                              # no quality record at all
        quality = ImageQualityDecision.objects.create(owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status="processing")
        self.assertEqual(self.rule(image), (False, "quality_verdict"))                              # still being decided
        ImageQualityDecision.objects.filter(pk=quality.pk).update(status="completed", decision="accepted")
        self.assertEqual(self.rule(image), (True, "accepted"))
        ImageQualityDecision.objects.filter(pk=quality.pk).update(decision="rejected")
        self.assertEqual(self.rule(image), (False, "rejected"))

    def test_a_rejected_face_check_or_upload_means_never_whatever_the_quality_says(self):
        self.assertEqual(self.rule(self.photo([a_face(1)], decision="rejected"), "accepted"), (False, "rejected"))
        self.assertEqual(self.rule(self.photo([a_face(1)], status="rejected"), "accepted"), (False, "rejected"))


class NeedsReviewTests(Pipeline):
    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(10)}

    def test_in_a_personal_workspace_nobody_can_decide_so_it_is_grouped_at_once(self):
        personal = create_organization(name="Me", owner=self.owner, is_personal=True)
        self.batch.organization = personal
        self.batch.save(update_fields=["organization"])
        image = self.photo([a_face(1)])
        self.check(image, "needs_review")
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")
        self.assertEqual(self.folders().count(), 1)

    def test_a_photo_in_no_organization_is_grouped_at_once(self):
        image = self.photo([a_face(1)])
        self.assertIsNone(self.batch.organization)
        self.check(image, "needs_review")
        self.assertEqual(self.folders().count(), 1)

    def test_in_an_organization_it_waits_for_a_moderator_and_is_grouped_when_one_approves(self):
        organization, moderator = self.organization_batch()
        image = self.photo([a_face(1)])
        self.check(image, "needs_review")
        self.assertFalse(IdentityAnalysis.objects.exists())                       # nothing yet
        self.assertFalse(IdentityGroup.objects.exists())
        review = ModeratorReview.objects.get(image=image, source="quality")
        self.assertEqual(verdict_allows_grouping(type(image).objects.get(pk=image.pk)), (False, "moderator_approval"))

        with self.model_patches(), self.captureOnCommitCallbacks(execute=True):
            response = self.authenticated_client(moderator).post(
                reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": review.pk}), {"action": "approve"}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ImageQualityDecision.objects.get(image=image).decision, "accepted")
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")
        self.assertEqual(self.folders().count(), 1)

    def test_a_moderator_who_rejects_it_leaves_it_ungrouped(self):
        organization, moderator = self.organization_batch()
        image = self.photo([a_face(1)])
        self.check(image, "needs_review")
        review = ModeratorReview.objects.get(image=image, source="quality")
        with self.captureOnCommitCallbacks(execute=True):
            self.authenticated_client(moderator).post(
                reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": review.pk}), {"action": "reject"}, format="json",
            )
        self.assertFalse(IdentityAnalysis.objects.exists())
        self.assertFalse(IdentityGroup.objects.exists())

    def test_a_moderator_who_only_keeps_it_in_review_groups_nothing(self):
        organization, moderator = self.organization_batch()
        image = self.photo([a_face(1)])
        self.check(image, "needs_review")
        review = ModeratorReview.objects.get(image=image, source="quality")
        with self.captureOnCommitCallbacks(execute=True):
            self.authenticated_client(moderator).post(
                reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": review.pk}), {"action": "keep_in_review"}, format="json",
            )
        self.assertFalse(IdentityAnalysis.objects.exists())

    def test_approving_twice_or_after_a_manual_analysis_does_not_group_it_a_second_time(self):
        organization, moderator = self.organization_batch()
        image = self.photo([a_face(1)])
        self.check(image, "needs_review")
        review = ModeratorReview.objects.get(image=image, source="quality")
        with self.model_patches(), self.captureOnCommitCallbacks(execute=True):
            self.authenticated_client(moderator).post(
                reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": review.pk}), {"action": "approve"}, format="json")
            from .verdict import group_after_verdict

            group_after_verdict(type(image).objects.get(pk=image.pk))
        self.assertEqual(IdentityAnalysis.objects.filter(image=image).count(), 1)
        self.assertEqual(IdentityMembership.objects.count(), 1)

    def test_a_moderator_approval_is_a_bulk_decision_too(self):
        organization, moderator = self.organization_batch()
        images = [self.photo([a_face(1)]), self.photo([a_face(2)])]
        for image in images:
            self.check(image, "needs_review")
        reviews = list(ModeratorReview.objects.filter(source="quality").values_list("pk", flat=True))
        with self.model_patches(), self.captureOnCommitCallbacks(execute=True):
            response = self.authenticated_client(moderator).post(
                reverse("moderation:review-bulk-decision", kwargs={"organization_id": organization.pk}), {"review_ids": reviews, "action": "approve"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(IdentityAnalysis.objects.count(), 2)
        self.assertEqual(self.folders().count(), 1)                              # the two photographs are one person


class VerdictOrderTests(Pipeline):
    def test_the_order_the_verdicts_arrive_in_does_not_change_the_final_folders(self):
        Provider.vectors = {1: angle(0), 2: angle(15), 3: angle(90), 4: angle(95), 5: angle(45)}
        faces = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "rejected-between": 5}

        def run(order, owner_number):
            """The same five photographs for a new owner, verdicts arriving in ``order``; returns the final folders as sets of photograph names."""
            from django.contrib.auth import get_user_model

            from privacy.testing import grant_face_consent
            from uploads.models import UploadBatch

            self.owner = get_user_model().objects.create_user(
                username=f"order-owner-{owner_number}", email=f"order-owner-{owner_number}@example.com", password="Password!123", is_email_verified=True,
            )
            grant_face_consent(self.owner)
            self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
            images = {name: self.photo([a_face(x)]) for name, x in faces.items()}
            for name in order:
                self.check(images[name], "rejected" if name == "rejected-between" else "accepted")
            names = {image.pk: name for name, image in images.items()}
            return {
                frozenset(names[m.identity_face.image_id] for m in folder.memberships.exclude(state__in=[State.REMOVED, State.REJECTED]).select_related("identity_face"))
                for folder in self.folders()
            }

        first = run(["a1", "a2", "b1", "b2", "rejected-between"], 1)
        second = run(["rejected-between", "b2", "a2", "b1", "a1"], 2)
        third = run(["b1", "rejected-between", "a2", "b2", "a1"], 3)

        self.assertEqual(first, {frozenset({"a1", "a2"}), frozenset({"b1", "b2"})})        # the rejected photograph, between the two people, is in neither
        self.assertEqual(second, first)
        self.assertEqual(third, first)


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class ManualAndRetryTests(Pipeline):
    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(10)}

    def test_the_manual_analysis_still_works_for_a_photo_without_a_verdict(self):
        from .services import enqueue_identity_analysis

        image = self.photo([a_face(1)])
        with patch("identities.services.get_identity_provider", return_value=self.provider), patch("identities.services._read_image", return_value=np.zeros((10, 10, 3), np.uint8)):
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_identity_analysis(image)
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")
        self.assertEqual(self.folders().count(), 1)

    def test_the_manual_endpoint_answers_for_a_photo_without_a_verdict(self):
        image = self.photo([a_face(1)])
        client = self.authenticated_client(self.owner)
        with patch("identities.services.get_identity_provider", return_value=self.provider), patch("identities.services._read_image", return_value=np.zeros((10, 10, 3), np.uint8)):
            with self.captureOnCommitCallbacks(execute=True):
                response = client.post(reverse("identities:image-analysis", kwargs={"image_id": image.pk}), {}, format="json")
        self.assertIn(response.status_code, (200, 202), response.content)
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")

    def test_the_manual_analysis_still_refuses_a_photo_the_checks_rejected(self):
        from rest_framework.exceptions import ValidationError

        from .services import enqueue_identity_analysis

        image = self.photo([a_face(1)], quality="rejected")
        with patch("identities.services.get_identity_provider", return_value=self.provider), self.assertRaises(ValidationError):
            enqueue_identity_analysis(image)

    def test_a_batch_retry_of_a_failed_quality_check_groups_the_photo_when_the_new_verdict_allows_it(self):
        from uploads.models import BatchImageProgress
        from uploads.services import retry_batch

        image = self.photo([a_face(1)])
        ImageQualityDecision.objects.create(
            owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status="failed", failure_reason="Quality analysis failed safely.",
        )
        BatchImageProgress.objects.filter(image=image).update(status="failed", failure_stage="quality_analysis", failure_reason="Quality analysis failed safely.")
        self.batch.processing_status = "failed"
        self.batch.save(update_fields=["processing_status"])
        with ExitStack() as stack:
            stack.enter_context(patch("quality_control.services._read_standardized", return_value=np.zeros((10, 10, 3), np.uint8)))
            stack.enter_context(patch("quality_control.services.analyze_image_quality", return_value={"defects": []}))
            stack.enter_context(patch("quality_control.services.with_face_sharpness", side_effect=lambda metrics, image, validation: metrics))
            stack.enter_context(patch("quality_control.services.calculate_scores", return_value={}))
            stack.enter_context(patch("quality_control.services.make_decision", return_value=("accepted", [], "test")))
            stack.enter_context(patch("identities.services.get_identity_provider", return_value=self.provider))
            stack.enter_context(patch("identities.services._read_image", return_value=np.zeros((10, 10, 3), np.uint8)))
            with self.captureOnCommitCallbacks(execute=True):
                retry_batch(self.batch)
        self.assertEqual(ImageQualityDecision.objects.get(image=image).decision, "accepted")
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")

    def test_preprocessing_no_longer_queues_the_identity_analysis(self):
        from django.core.files.base import ContentFile
        from PIL import Image
        import io

        from uploads.models import ImageUpload
        from uploads.services import preprocess_image

        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 130, 140)).save(buffer, format="PNG")
        image = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name="raw.png", file_extension=".png", mime_type="image/png", status="uploaded", width=64, height=64)
        image.original_file.save("raw.png", ContentFile(buffer.getvalue()), save=True)
        with patch("validation.services.validate_image_faces_safely"), patch("quality_control.services.enqueue_quality_analysis") as quality, \
                patch("identities.services.enqueue_identity_analysis") as identity:
            preprocess_image(image)
        quality.assert_called_once()
        identity.assert_not_called()


class ProgressTests(Pipeline):
    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(10)}

    def grouping(self, image):
        payload = batch_progress_payload(self.batch)
        return next(row["identity_grouping"] for row in payload["images"] if row["image_id"] == image.pk)

    def test_a_photo_waiting_for_its_verdict_shows_grouping_as_queued_not_done(self):
        image = self.photo([a_face(1)])
        ImageQualityDecision.objects.create(owner=self.owner, image=image, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, status="queued")
        self.assertEqual(self.grouping(image), {"status": "queued", "waiting_for": "quality_verdict"})

    def test_a_photo_with_no_quality_record_yet_is_waiting_for_its_verdict_too(self):
        self.assertEqual(self.grouping(self.photo([a_face(1)])), {"status": "queued", "waiting_for": "quality_verdict"})

    def test_it_moves_to_queued_then_completed_once_the_verdict_allows_it(self):
        image = self.photo([a_face(1)])
        self.check(image, "accepted", execute=False)
        sync_image_progress(image.pk)
        self.assertEqual(self.grouping(image), {"status": "queued", "waiting_for": ""})
        progress = image.processing_progress
        progress.refresh_from_db()
        self.assertEqual((progress.stage, progress.status), ("identity_grouping", "queued"))        # not finished: the photograph is still being grouped
        self.assertNotIn(self.batch.__class__.objects.get(pk=self.batch.pk).processing_status, ("completed", "partial"))
        with self.model_patches(), self.captureOnCommitCallbacks(execute=True):
            from .tasks import analyze_image_identity_task

            queued = IdentityAnalysis.objects.get(image=image)
            analyze_image_identity_task.apply(args=[queued.pk], task_id=queued.task_id)
        self.assertEqual(self.grouping(image), {"status": "completed", "waiting_for": ""})

    def test_a_rejected_photo_is_skipped_and_never_waits(self):
        image = self.photo([a_face(1)])
        self.check(image, "rejected")
        self.assertEqual(self.grouping(image), {"status": "skipped", "waiting_for": ""})

    def test_a_photo_held_for_a_moderator_says_so(self):
        self.organization_batch()
        image = self.photo([a_face(1)])
        self.check(image, "needs_review")
        self.assertEqual(self.grouping(image), {"status": "queued", "waiting_for": "moderator_approval"})

    def test_without_automatic_grouping_it_is_not_applicable(self):
        image = self.photo([a_face(1)], quality="accepted")
        with override_settings(IDENTITY_AUTO_ANALYZE=False):
            self.assertEqual(self.grouping(image), {"status": "not_applicable", "waiting_for": ""})

    def test_the_status_words_for_every_analysis_state(self):
        image = self.photo([a_face(1)], quality="accepted")
        analysis = IdentityAnalysis.objects.create(image=image, owner=self.owner, preprocessing_result=image.preprocessing_result, face_validation=image.face_validation, provider="x", policy_version="y")
        for stored, word in (("pending", "queued"), ("queued", "queued"), ("processing", "processing"), ("completed", "completed"), ("needs_review", "needs_review"),
                             ("failed", "failed"), ("cancelled", "cancelled")):
            IdentityAnalysis.objects.filter(pk=analysis.pk).update(status=stored)
            image = type(image).objects.get(pk=image.pk)
            self.assertEqual(grouping_status(image, personal=True)["status"], word, stored)


class SuggestedMergesTests(Pipeline):
    """Photographs 1, 2 and 3 arrive in that order: 0 degrees opens a folder, 80 degrees is too far from it so it opens another, and 35 degrees then joins the first,
    which moves its average to 17.5 degrees: 62.5 degrees from the second folder, a similarity of 0.46, above the 0.40 merge limit."""

    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(80), 3: angle(35), 4: vec(0, 0, 1)}

    def batch_of_three(self):
        images = [self.photo([a_face(x)]) for x in (1, 2, 3)]
        for image in images:
            self.check(image, "accepted")
        return images

    def merge_notes(self, user=None):
        return list(UserNotification.objects.filter(recipient=user or self.owner, event_type=MERGE_EVENT))

    def test_two_folders_from_one_batch_that_match_are_suggested_and_the_owner_is_told_once(self):
        self.batch_of_three()
        self.assertEqual(self.folders().count(), 2)

        (suggestion,) = suggested_merges(self.owner)
        first, second = sorted(self.folders(), key=lambda folder: -folder.member_count)
        self.assertEqual((suggestion["source_group"], suggestion["target_group"]), (second.pk, first.pk))     # the smaller folder is merged into the larger
        self.assertAlmostEqual(suggestion["similarity"], 0.4617, places=3)
        self.assertEqual(suggestion["target"]["member_count"], 2)

        (note,) = self.merge_notes()
        self.assertEqual(note.message, "2 of your person folders may be the same person. Check them and merge the ones that are.")
        self.assertEqual((note.severity, note.resource_type, note.resource_id, note.action_path), ("info", "batch", self.batch.pk, "/dashboard/groups"))
        self.assertEqual((note.metadata["batch_id"], note.metadata["folder_count"], note.metadata["suggested_merge_count"]), (self.batch.pk, 2, 1))
        self.assertEqual(note.email_status, "skipped")                                                    # in-app only

    def test_however_often_it_is_checked_the_owner_is_told_once_per_batch(self):
        self.batch_of_three()
        for _ in range(3):
            announce_suggested_merges(self.batch.pk)
        self.assertEqual(len(self.merge_notes()), 1)

    def test_the_endpoint_lists_the_owners_pairs_and_nothing_is_merged_for_them(self):
        self.batch_of_three()
        client = self.authenticated_client(self.owner)
        listed = client.get(reverse("identities:group-suggested-merges"))
        self.assertEqual(listed.status_code, 200, listed.content)
        (row,) = listed.json()
        self.assertEqual(set(row), {"source_group", "target_group", "similarity", "source", "target"})
        self.assertEqual(self.folders().count(), 2)                                                       # still two folders

        merged = client.post(reverse("identities:group-merge", kwargs={"pk": row["target_group"]}), {"source_group_id": row["source_group"]}, format="json")
        self.assertEqual(merged.status_code, 200, merged.content)
        self.assertEqual(self.folders().count(), 1)
        self.assertEqual(client.get(reverse("identities:group-suggested-merges")).json(), [])             # nothing left to suggest

    def test_the_endpoint_needs_a_signed_in_person(self):
        self.assertEqual(self.client.get(reverse("identities:group-suggested-merges")).status_code, 401)

    def test_another_persons_folders_are_never_suggested_to_this_owner_or_merged_with_theirs(self):
        self.batch_of_three()
        stranger = self.make_user("merge-stranger", email="merge-stranger@example.com")
        look_alike = IdentityGroup.objects.create(
            owner=stranger, display_name="Person 001", embedding_dimension=3, provider=self.provider.name, model_checksum="abc", policy_version="test",
            protected_centroid=self.folders().first().protected_centroid, member_count=5,
        )
        pairs = suggested_merges(self.owner)
        self.assertEqual(len(pairs), 1)
        self.assertNotIn(look_alike.pk, {pairs[0]["source_group"], pairs[0]["target_group"]})
        self.assertEqual(suggested_merges(stranger), [])
        self.assertEqual(self.authenticated_client(stranger).get(reverse("identities:group-suggested-merges")).json(), [])
        self.assertEqual(self.merge_notes(stranger), [])

    def test_folders_that_do_not_match_give_no_suggestion_and_no_notification(self):
        images = [self.photo([a_face(1)]), self.photo([a_face(4)])]                                     # 90 degrees apart: two different people
        for image in images:
            self.check(image, "accepted")
        self.assertEqual(self.folders().count(), 2)
        self.assertEqual(suggested_merges(self.owner), [])
        self.assertEqual(self.merge_notes(), [])
        self.assertEqual(batch_progress_payload(self.batch)["suggested_merge_count"], 0)

    def test_one_folder_gives_nothing_to_suggest(self):
        self.check(self.photo([a_face(1)]), "accepted")
        self.assertEqual(suggested_merges(self.owner), [])
        self.assertEqual(self.merge_notes(), [])

    def test_the_switch_for_new_person_folders_also_silences_it(self):
        NotificationPreference.objects.create(user=self.owner, person_group_created=False)
        self.batch_of_three()
        self.assertEqual(self.merge_notes(), [])
        self.assertEqual(len(suggested_merges(self.owner)), 1)                                           # the suggestion itself is still there

    def test_the_progress_counts_the_batchs_suggestions_once_it_is_fully_grouped(self):
        payload_before = batch_progress_payload(self.batch)
        self.assertEqual((payload_before["identity_grouping_finished"], payload_before["suggested_merge_count"]), (False, 0))
        self.batch_of_three()
        payload = batch_progress_payload(self.batch)
        self.assertEqual((payload["identity_grouping_finished"], payload["suggested_merge_count"]), (True, 1))

    def test_nothing_is_announced_while_a_photograph_is_still_waiting_for_its_verdict(self):
        waiting = self.photo([a_face(1)])
        ImageQualityDecision.objects.create(owner=self.owner, image=waiting, preprocessing_result=waiting.preprocessing_result, face_validation=waiting.face_validation, status="processing")
        for x in (2, 3):
            self.check(self.photo([a_face(x)]), "accepted")
        # photographs 2 and 3 are grouped, but one of the batch is not decided yet
        self.assertFalse(batch_grouping_finished(self.batch))
        self.assertEqual(self.merge_notes(), [])
        self.assertEqual(batch_progress_payload(self.batch)["suggested_merge_count"], 0)

    def test_a_photograph_waiting_for_a_moderator_does_not_hold_the_batch_back(self):
        self.organization_batch()
        for x in (1, 2, 3):
            self.check(self.photo([a_face(x)]), "accepted")
        self.check(self.photo([a_face(4)]), "needs_review")
        self.assertTrue(batch_grouping_finished(self.batch))
        self.assertEqual(len(self.merge_notes()), 1)

    def test_the_last_photograph_being_rejected_is_what_finishes_the_batch(self):
        last = self.photo([a_face(4)])
        ImageQualityDecision.objects.create(owner=self.owner, image=last, preprocessing_result=last.preprocessing_result, face_validation=last.face_validation, status="processing")
        for x in (1, 2, 3):
            self.check(self.photo([a_face(x)]), "accepted")
        self.assertEqual(self.merge_notes(), [])                                                         # the last photograph is still undecided
        self.check(last, "rejected")
        self.assertEqual(len(self.merge_notes()), 1)

    def test_only_the_suggestions_that_involve_this_batch_are_its_news(self):
        # an older pair (folders of an earlier batch) is in the owner's list but not in this batch's count or notification
        old_batch = type(self.batch).objects.create(owner=self.owner, upload_type="multiple")
        self.batch, kept = old_batch, self.batch
        for x in (1, 2, 3):
            self.check(self.photo([a_face(x)]), "accepted")
        self.batch = kept
        UserNotification.objects.all().delete()
        self.check(self.photo([a_face(4)]), "accepted")                                                  # a new, unrelated person
        self.assertEqual(len(suggested_merges(self.owner)), 1)                                           # the old pair is still suggested
        self.assertEqual(self.merge_notes(), [])                                                         # but this batch has nothing new to say
        self.assertEqual(batch_progress_payload(self.batch)["suggested_merge_count"], 0)

    def test_folders_made_by_different_models_are_not_compared(self):
        self.batch_of_three()
        IdentityGroup.objects.filter(owner=self.owner).update(model_checksum="another-model")
        first = self.folders().order_by("pk").first()
        IdentityGroup.objects.filter(pk=first.pk).update(model_checksum="abc")
        self.assertEqual(suggested_merges(self.owner), [])


class RejectedLeavesNothingBehindTests(Pipeline):
    """The food photograph: a detection that looked like a face opened a folder (and a review), and the quality check then rejected the photograph."""

    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(10), 3: angle(90)}

    def grouped_then_rejected(self, folder_name_after=None):
        image = self.photo([a_face(3)])
        self.analyse(image)                                      # grouped early, as a manual analysis or the old order did
        return image

    def test_the_folder_only_that_photo_made_is_archived_and_its_open_review_is_closed(self):
        organization, _moderator = self.organization_batch()
        keeper = self.photo([a_face(1)])
        self.analyse(keeper)
        food = self.photo([a_face(3)])
        # the food photograph sits between nothing: it opened its own folder, and the ambiguous second membership raised an identity review
        self.analyse(food)
        folder = IdentityMembership.objects.get(identity_face__image=food).identity_group
        membership = IdentityMembership.objects.get(identity_face__image=food)
        review = ModeratorReview.objects.create(
            organization=organization, image=food, source="identity", status="pending", ai_recommendation="ambiguous", identity_membership=membership,
        )
        other_review = ModeratorReview.objects.create(
            organization=organization, image=keeper, source="identity", status="pending", ai_recommendation="ambiguous",
            identity_membership=IdentityMembership.objects.get(identity_face__image=keeper),
        )

        self.check(food, "rejected")

        folder.refresh_from_db()
        self.assertEqual(folder.state, IdentityGroup.State.ARCHIVED)
        membership.refresh_from_db()
        self.assertEqual(membership.state, State.REMOVED)
        review.refresh_from_db()
        self.assertEqual((review.status, review.decision_reason, review.decided_by_id), ("rejected", "Photo was rejected", None))
        self.assertIsNotNone(review.decided_at)
        self.assertFalse(review.is_open)
        other_review.refresh_from_db()
        self.assertTrue(other_review.is_open)                                                          # another photograph's review is untouched
        self.assertEqual(self.folders().count(), 1)                                                    # only the keeper's folder is left
        self.assertTrue(IdentityChange.objects.filter(group=folder, action="archived").exists())

    def test_a_folder_that_other_photographs_share_stays_active(self):
        first, second = self.photo([a_face(1)]), self.photo([a_face(2)])
        self.analyse(first)
        self.analyse(second)
        (folder,) = self.folders()
        self.check(second, "rejected")
        folder.refresh_from_db()
        self.assertEqual((folder.state, folder.member_count), (IdentityGroup.State.ACTIVE, 1))

    def test_a_folder_the_owner_named_is_kept_even_when_it_is_left_empty(self):
        image = self.grouped_then_rejected()
        folder = IdentityMembership.objects.get(identity_face__image=image).identity_group
        folder.display_name = "Grandma"
        folder.save(update_fields=["display_name"])
        self.check(image, "rejected")
        folder.refresh_from_db()
        self.assertEqual(folder.state, IdentityGroup.State.ACTIVE)

    def test_a_review_somebody_already_decided_is_not_reopened_or_changed(self):
        organization, moderator = self.organization_batch()
        image = self.photo([a_face(3)])
        self.analyse(image)
        membership = IdentityMembership.objects.get(identity_face__image=image)
        decided = ModeratorReview.objects.create(
            organization=organization, image=image, source="identity", status="approved", ai_recommendation="x", identity_membership=membership,
            decided_by=moderator, decision_reason="Looks right",
        )
        ModeratorReview.objects.filter(pk=decided.pk).update(decided_at=membership.created_at)
        self.check(image, "rejected")
        decided.refresh_from_db()
        self.assertEqual((decided.status, decided.decision_reason, decided.decided_by_id), ("approved", "Looks right", moderator.pk))

    def test_only_identity_reviews_are_closed_the_quality_review_is_left_to_its_own_decision(self):
        organization, _moderator = self.organization_batch()
        image = self.photo([a_face(3)])
        self.analyse(image)
        quality = ModeratorReview.objects.create(organization=organization, image=image, source="duplicate", status="pending", ai_recommendation="x")
        self.check(image, "rejected")
        quality.refresh_from_db()
        self.assertTrue(quality.is_open)

    def test_a_moderator_rejecting_the_quality_review_takes_a_grouped_photo_out_too(self):
        organization, moderator = self.organization_batch()
        image = self.photo([a_face(3)])
        self.check(image, "needs_review")
        self.assertFalse(IdentityAnalysis.objects.exists())                                            # held back, as it should be
        self.analyse(image)                                                                            # but somebody grouped it by hand
        membership = IdentityMembership.objects.get(identity_face__image=image)
        folder = membership.identity_group
        identity_review = ModeratorReview.objects.create(organization=organization, image=image, source="identity", status="pending", ai_recommendation="x", identity_membership=membership)
        quality_review = ModeratorReview.objects.get(image=image, source="quality")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.authenticated_client(moderator).post(
                reverse("moderation:review-decision", kwargs={"organization_id": organization.pk, "review_id": quality_review.pk}), {"action": "reject"}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        membership.refresh_from_db()
        folder.refresh_from_db()
        identity_review.refresh_from_db()
        self.assertEqual((membership.state, folder.state), (State.REMOVED, IdentityGroup.State.ARCHIVED))
        self.assertEqual((identity_review.status, identity_review.decision_reason), ("rejected", "Photo was rejected"))

    def test_a_system_closing_is_not_counted_as_a_moderators_decision(self):
        from moderation.services import dashboard_summary  # noqa: F401  (the per-moderator figures only count reviews a person decided)

        organization, _moderator = self.organization_batch()
        image = self.photo([a_face(3)])
        self.analyse(image)
        membership = IdentityMembership.objects.get(identity_face__image=image)
        review = ModeratorReview.objects.create(organization=organization, image=image, source="identity", status="pending", ai_recommendation="x", identity_membership=membership)
        self.check(image, "rejected")
        review.refresh_from_db()
        self.assertIsNone(review.decided_by_id)

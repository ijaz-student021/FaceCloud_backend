"""The administrator's failure screens: who, which organization, where and why it failed, when, and whether a batch can be retried now.

``GET /api/system/batches/failed/``, ``GET /api/system/jobs/failed/``, ``POST /api/system/batches/{id}/retry/`` and the rows of
``GET /api/system/moderation/``. Everything added is additive: the keys that were there are checked to be unchanged.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from collages.models import Collage
from enhancement.models import ImageEnhancementResult
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import BatchImageProgress, ImagePreprocessingResult, ImageUpload, UploadBatch
from uploads.progress import sync_image_progress

from .failures import describe_failed_batch
from .jobs import JOB_KINDS, list_failed_jobs

User = get_user_model()

OLD_BATCH_KEYS = {
    "id", "owner_id", "owner_username", "upload_type", "display_name", "processing_status", "current_stage", "total_files", "accepted_files",
    "processed_files", "processing_failed_files", "processing_error", "retry_count", "processing_started_at", "processing_finished_at",
    "created_at", "updated_at",
}
NEW_BATCH_KEYS = {
    "organization_id", "organization_name", "failure_stage", "failure_stage_label", "failure_reason", "failed_images", "failed_at",
    "can_retry", "retry_blocked_reason", "max_retries", "retries_left",
}
OLD_JOB_KEYS = {"id", "owner_id", "owner_username", "reason", "created_at", "updated_at"}
NEW_JOB_KEYS = {
    "kind", "kind_label", "organization_id", "organization_name", "failure_stage", "failure_stage_label", "failure_reason", "failed_at",
}


class Fixture:
    def make_user(self, name, **extra):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821", is_email_verified=True, **extra,
        )

    def setUp(self):
        self.admin = self.make_user("failures-admin", role="admin")
        self.owner = self.make_user("failures-owner")
        self.organization = create_organization(name="Failures Co", owner=self.owner)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def failed_batch(self, owner=None, organization="default", photos=(("preprocessing", "The stored image could not be safely preprocessed."),),
                     **batch_fields):
        """A batch that has failed: one photograph per ``(stage, reason)``, each recorded as failed at that stage."""
        owner = owner or self.owner
        batch = UploadBatch.objects.create(
            owner=owner, upload_type="multiple", processing_status="failed", current_stage="completed",
            organization=self.organization if organization == "default" else organization, **batch_fields,
        )
        for index, (stage, reason) in enumerate(photos):
            image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=f"p{index}.png", status="uploaded")
            BatchImageProgress.objects.filter(image=image).update(
                status="failed", failure_stage=stage, failure_reason=reason, finished_at=timezone.now() - timedelta(minutes=10 - index),
            )
        UploadBatch.objects.filter(pk=batch.pk).update(processing_status="failed", current_stage="completed")
        batch.refresh_from_db()
        return batch

    def listed(self, **query):
        response = self.client.get(reverse("operations:failed-batches"), query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def row(self, batch):
        return next(row for row in self.listed() if row["id"] == batch.pk)


class FailedBatchRowTests(Fixture, TestCase):
    def test_a_row_says_who_which_organization_where_why_when_and_whether_to_retry(self):
        batch = self.failed_batch(photos=(("face_validation", "The face check could not run on this photo."),) * 2, retry_count=1)

        row = self.row(batch)

        self.assertEqual((row["owner_id"], row["owner_username"]), (self.owner.pk, "failures-owner"))
        self.assertEqual((row["organization_id"], row["organization_name"]), (self.organization.pk, "Failures Co"))
        self.assertEqual((row["failure_stage"], row["failure_stage_label"]), ("face_validation", "Face validation"))
        self.assertEqual(row["failure_reason"], "The face check could not run on this photo.")
        self.assertEqual(row["failed_images"], 2)
        self.assertTrue(row["failed_at"])
        self.assertEqual((row["can_retry"], row["retry_blocked_reason"], row["max_retries"], row["retries_left"], row["retry_count"]), (True, "", 3, 2, 1))

    def test_nothing_that_was_there_is_removed_or_renamed(self):
        row = self.row(self.failed_batch())
        self.assertTrue(OLD_BATCH_KEYS <= set(row), OLD_BATCH_KEYS - set(row))
        self.assertTrue(NEW_BATCH_KEYS <= set(row), NEW_BATCH_KEYS - set(row))

    def test_the_stage_is_where_the_photographs_failed_not_the_batchs_own_finished_stage(self):
        batch = self.failed_batch(photos=(("enhancement", "The background enhancement queue is unavailable."),))
        row = self.row(batch)
        self.assertEqual(row["current_stage"], "completed")                    # what the batch itself says once every photograph is finished
        self.assertEqual(row["failure_stage"], "enhancement")

    def test_with_photographs_failing_at_different_stages_the_most_common_wins_and_a_tie_goes_to_the_earlier_stage(self):
        majority = self.failed_batch(photos=(("enhancement", "a"), ("quality_analysis", "b"), ("quality_analysis", "b")))
        self.assertEqual(self.row(majority)["failure_stage"], "quality_analysis")
        tie = self.failed_batch(photos=(("enhancement", "a"), ("preprocessing", "b")))
        self.assertEqual(self.row(tie)["failure_stage"], "preprocessing")

    def test_the_most_common_reason_is_given_and_it_is_always_a_sentence(self):
        common = self.failed_batch(photos=(("enhancement", "Rare."), ("enhancement", "Usual."), ("enhancement", "Usual.")))
        self.assertEqual(self.row(common)["failure_reason"], "Usual.")
        unrecorded = self.failed_batch(photos=(("quality_analysis", ""),))
        self.assertEqual(self.row(unrecorded)["failure_reason"], "Quality analysis did not finish.")

    def test_a_batch_that_failed_before_any_photograph_did_uses_the_batch_level_error_and_cannot_be_retried(self):
        batch = self.failed_batch(photos=(), processing_error="The background processing queue is unavailable.")
        UploadBatch.objects.filter(pk=batch.pk).update(current_stage="queued")

        row = self.row(batch)

        self.assertEqual(row["failure_reason"], "The background processing queue is unavailable.")
        self.assertEqual((row["failure_stage"], row["failed_images"]), ("queued", 0))
        self.assertFalse(row["can_retry"])
        self.assertEqual(row["retry_blocked_reason"], "This batch has no failed images to retry.")

    def test_a_batch_with_nothing_recorded_at_all_still_gets_a_sentence(self):
        batch = self.failed_batch(photos=())
        self.assertEqual(self.row(batch)["failure_reason"], "The batch failed, but no reason was recorded.")

    @override_settings(BATCH_PROGRESS_MAX_RETRIES=2)
    def test_a_batch_out_of_retries_says_so(self):
        batch = self.failed_batch(retry_count=2)
        row = self.row(batch)
        self.assertEqual((row["can_retry"], row["retries_left"], row["max_retries"]), (False, 0, 2))
        self.assertEqual(row["retry_blocked_reason"], "The maximum of 2 batch retries has been reached.")

    def test_failed_at_is_when_the_photographs_failed_else_when_the_batch_finished_else_its_last_change(self):
        batch = self.failed_batch(photos=(("enhancement", "x"), ("enhancement", "x")))
        latest = BatchImageProgress.objects.filter(batch=batch).order_by("-finished_at").first().finished_at
        self.assertEqual(describe_failed_batch(batch)["failed_at"], latest)
        BatchImageProgress.objects.filter(batch=batch).update(finished_at=None)
        finished = timezone.now() - timedelta(days=1)
        UploadBatch.objects.filter(pk=batch.pk).update(processing_finished_at=finished)
        batch.refresh_from_db()
        self.assertEqual(describe_failed_batch(batch)["failed_at"], finished)
        UploadBatch.objects.filter(pk=batch.pk).update(processing_finished_at=None)
        batch.refresh_from_db()
        self.assertEqual(describe_failed_batch(batch)["failed_at"], batch.updated_at)


class FailedBatchOrganizationTests(Fixture, TestCase):
    def test_a_batch_uploaded_to_another_organization_shows_that_one(self):
        other = create_organization(name="Second Co", owner=self.make_user("second-owner"))
        OrganizationMembership.objects.create(organization=other, user=self.owner, role="member", status="active")
        batch = self.failed_batch(organization=other)
        row = self.row(batch)
        self.assertEqual((row["organization_id"], row["organization_name"]), (other.pk, "Second Co"))

    def test_a_batch_never_filed_under_an_organization_shows_the_owners_own_workspace(self):
        batch = self.failed_batch(organization=None)
        row = self.row(batch)
        self.assertEqual((row["organization_id"], row["organization_name"]), (self.organization.pk, "Failures Co"))

    def test_an_owner_with_no_organization_at_all_shows_null_not_an_error(self):
        loner = self.make_user("failures-loner")
        batch = self.failed_batch(owner=loner, organization=None)
        row = self.row(batch)
        self.assertEqual((row["organization_id"], row["organization_name"]), (None, None))

    def test_a_page_of_many_batches_costs_no_more_queries_than_one(self):
        self.failed_batch()
        with CaptureQueriesContext(connection) as one:
            self.listed()
        for _ in range(6):
            self.failed_batch(photos=(("enhancement", "x"), ("preprocessing", "y")))
        with CaptureQueriesContext(connection) as many:
            rows = self.listed()
        self.assertEqual(len(rows), 7)
        self.assertEqual(len(many), len(one))


class CanRetryAgreesWithRetryTests(Fixture, TestCase):
    """The list says "retry" is possible exactly when pressing it works."""

    def retry(self, batch):
        return self.client.post(reverse("operations:admin-batch-retry", kwargs={"batch_id": batch.pk}))

    @override_settings(BATCH_PROGRESS_MAX_RETRIES=1)
    @patch("uploads.services.enqueue_batch_preprocessing")
    def test_every_situation(self, _enqueue):
        situations = {
            "failed photographs and retries left": self.failed_batch(),
            "out of retries": self.failed_batch(retry_count=1),
            "no failed photographs": self.failed_batch(photos=()),
        }
        for name, batch in situations.items():
            with self.subTest(name):
                promised = self.row(batch)["can_retry"]
                response = self.retry(batch)
                self.assertEqual(response.status_code == 202, promised, response.content)
                self.assertEqual(response.status_code in (202, 400), True)


class RetryAnswerTests(Fixture, TestCase):
    @patch("uploads.services.enqueue_batch_preprocessing")
    def test_the_answer_carries_the_batchs_new_status_and_the_retries_that_are_left(self, enqueue):
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single", organization=self.organization)
        image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="a.png", status="uploaded")
        ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="failed", failure_reason="decoder failed")
        sync_image_progress(image.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.processing_status, "failed")

        response = self.client.post(reverse("operations:admin-batch-retry", kwargs={"batch_id": batch.pk}))

        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        batch.refresh_from_db()
        self.assertEqual(body["status"], "queued")                              # the existing key
        self.assertEqual(body["processing_status"], "queued")                   # the name the failed-batch list uses
        self.assertEqual(body["processing_status"], batch.processing_status)    # and it is what was saved
        self.assertEqual((body["batch_id"], body["retry_count"], body["max_retries"], body["retries_left"]), (batch.pk, 1, 3, 2))
        enqueue.assert_called_once()
        listed = [row["id"] for row in self.listed()]
        self.assertNotIn(batch.pk, listed)                                      # no longer failed, so no longer in the failed list


class FailedJobRowTests(Fixture, TestCase):
    def failed_enhancement(self, batch, reason="boom"):
        image = batch.images.first()
        preprocessing = ImagePreprocessingResult.objects.create(owner=batch.owner, image=image, status="completed", output_format="png")
        return ImageEnhancementResult.objects.create(
            owner=batch.owner, image=image, preprocessing_result=preprocessing, status="failed", failure_reason=reason,
            finished_at=timezone.now() - timedelta(hours=1),
        )

    def rows(self, kind):
        response = self.client.get(reverse("operations:failed-jobs"), {"kind": kind})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["kinds"][kind]

    def test_a_failed_enhancement_names_the_owner_the_batchs_organization_the_stage_the_reason_and_the_time(self):
        other = create_organization(name="Job Co", owner=self.make_user("job-owner"))
        OrganizationMembership.objects.create(organization=other, user=self.owner, role="member", status="active")
        batch = self.failed_batch(organization=other)
        result = self.failed_enhancement(batch, reason="The enhancement could not be finished safely.")

        (row,) = self.rows("enhancement")

        self.assertEqual((row["id"], row["owner_id"], row["owner_username"]), (result.pk, self.owner.pk, "failures-owner"))
        self.assertEqual((row["organization_id"], row["organization_name"]), (other.pk, "Job Co"))
        self.assertEqual((row["kind"], row["kind_label"]), ("enhancement", "Enhancement"))
        self.assertEqual((row["failure_stage"], row["failure_stage_label"]), ("enhancement", "Enhancement"))
        self.assertEqual((row["reason"], row["failure_reason"]), ("The enhancement could not be finished safely.",) * 2)
        self.assertEqual(row["failed_at"], result.finished_at.isoformat().replace("+00:00", "Z"))

    def test_nothing_that_was_there_is_removed_or_renamed(self):
        self.failed_enhancement(self.failed_batch())
        (row,) = self.rows("enhancement")
        self.assertTrue(OLD_JOB_KEYS <= set(row), OLD_JOB_KEYS - set(row))
        self.assertTrue(NEW_JOB_KEYS <= set(row), NEW_JOB_KEYS - set(row))

    def test_a_failure_with_no_recorded_reason_still_reads_as_a_sentence(self):
        self.failed_enhancement(self.failed_batch(), reason="")
        (row,) = self.rows("enhancement")
        self.assertEqual(row["reason"], "")                                       # the stored text, untouched
        self.assertEqual(row["failure_reason"], "Enhancement failed, but no reason was recorded.")

    def test_a_job_with_no_batch_shows_the_owners_own_workspace(self):
        collage = Collage.objects.create(
            owner=self.owner, source_type="event", source_ref=1, status="failed", failure_reason="The collage could not be made.",
        )
        (row,) = self.rows("collage")
        self.assertEqual(row["id"], collage.pk)
        self.assertEqual((row["organization_id"], row["organization_name"]), (self.organization.pk, "Failures Co"))
        self.assertEqual((row["failure_stage"], row["failure_stage_label"]), ("collage", "Collage"))

    def test_a_failed_upload_batch_here_reads_like_the_failed_batch_list_including_whether_it_can_be_retried(self):
        batch = self.failed_batch(photos=(("quality_analysis", "Quality analysis failed safely."),))
        (row,) = self.rows("upload_batch")
        listed = self.row(batch)
        for key in ("failure_stage", "failure_stage_label", "failure_reason", "failed_images", "failed_at", "can_retry", "retry_blocked_reason", "retries_left"):
            self.assertEqual(row[key], listed[key], key)
        self.assertEqual((row["organization_id"], row["kind"]), (self.organization.pk, "upload_batch"))

    def test_every_job_kind_has_a_label_and_a_stage_except_the_batch_which_works_its_stage_out(self):
        for job_kind in JOB_KINDS:
            with self.subTest(job_kind.key):
                self.assertTrue(job_kind.label)
                self.assertEqual(bool(job_kind.stage), job_kind.key != "upload_batch")

    def test_every_kind_lists_without_error_when_something_has_failed(self):
        self.failed_enhancement(self.failed_batch())
        payload = list_failed_jobs()
        self.assertTrue(payload["kinds"]["enhancement"])
        self.assertEqual(set(payload["kinds"]), {job_kind.key for job_kind in JOB_KINDS})


class ModerationOverviewRowsTests(Fixture, TestCase):
    """The moderation overview has no failure rows: its rows are organizations and moderators. Their identifying keys are pinned here."""

    def test_the_rows_name_each_organization_and_moderator_by_id_and_name(self):
        from moderation.models import ModeratorReview

        moderator = self.make_user("overview-moderator")
        OrganizationMembership.objects.create(organization=self.organization, user=moderator, role="moderator", status="active")
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single", organization=self.organization)
        image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="a.png", status="uploaded")
        ModeratorReview.objects.create(
            organization=self.organization, image=image, source="quality", ai_recommendation="needs_review", assigned_moderator=moderator,
        )

        body = self.client.get(reverse("operations:admin-moderation-overview")).json()

        (organization_row,) = body["organizations_needing_attention"]
        self.assertEqual((organization_row["organization_id"], organization_row["organization_name"]), (self.organization.pk, "Failures Co"))
        (moderator_row,) = body["moderator_workload"]
        self.assertEqual((moderator_row["moderator_id"], moderator_row["username"]), (moderator.pk, "overview-moderator"))

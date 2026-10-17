from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from enhancement.models import ImageEnhancementResult
from uploads.models import (
    BatchImageProgress,
    ImagePreprocessingResult,
    ImageUpload,
    UploadBatch,
)

from .jobs import InvalidJobKind, list_failed_jobs

User = get_user_model()


class AdminManagementFixtureMixin:
    def make_user(self, name, **extra):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821",
            is_email_verified=True, **extra,
        )

    def make_image(self, owner, batch):
        return ImageUpload.objects.create(
            owner=owner, batch=batch, original_name="a.png", status="uploaded",
        )


class ListFailedJobsTests(AdminManagementFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("failed-jobs-owner")
        self.other = self.make_user("failed-jobs-other")
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        self.image = self.make_image(self.owner, self.batch)
        self.pre = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.image, status="completed", output_format="png",
        )

    def test_only_failed_rows_are_returned_with_owner_and_reason(self):
        ImageEnhancementResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.pre,
            status="failed", failure_reason="boom",
        )

        result = list_failed_jobs()

        row = result["kinds"]["enhancement"][0]
        self.assertEqual(row["owner_id"], self.owner.pk)
        self.assertEqual(row["owner_username"], self.owner.username)
        self.assertEqual(row["reason"], "boom")

    def test_non_failed_rows_are_excluded(self):
        ImageEnhancementResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.pre, status="completed",
        )

        result = list_failed_jobs()

        self.assertEqual(result["kinds"]["enhancement"], [])

    def test_kind_filter_restricts_to_one_kind_and_others_are_absent(self):
        ImageEnhancementResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.pre,
            status="failed", failure_reason="x",
        )

        result = list_failed_jobs(kind="enhancement")

        self.assertEqual(list(result["kinds"]), ["enhancement"])

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(InvalidJobKind):
            list_failed_jobs(kind="not-a-real-kind")

    def test_owner_filter_restricts_across_all_kinds(self):
        image_two = self.make_image(self.other, UploadBatch.objects.create(owner=self.other, upload_type="single"))
        pre_two = ImagePreprocessingResult.objects.create(
            owner=self.other, image=image_two, status="completed", output_format="png",
        )
        ImageEnhancementResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.pre,
            status="failed", failure_reason="mine",
        )
        ImageEnhancementResult.objects.create(
            owner=self.other, image=image_two, preprocessing_result=pre_two,
            status="failed", failure_reason="theirs",
        )

        result = list_failed_jobs(owner=self.owner.pk)

        self.assertEqual(len(result["kinds"]["enhancement"]), 1)
        self.assertEqual(result["kinds"]["enhancement"][0]["reason"], "mine")

    def test_limit_per_kind_is_bounded_and_respected(self):
        for index in range(3):
            image = self.make_image(self.owner, self.batch)
            pre = ImagePreprocessingResult.objects.create(
                owner=self.owner, image=image, status="completed", output_format="png",
            )
            ImageEnhancementResult.objects.create(
                owner=self.owner, image=image, preprocessing_result=pre,
                status="failed", failure_reason=f"reason-{index}",
            )

        result = list_failed_jobs(limit_per_kind=2)

        self.assertEqual(len(result["kinds"]["enhancement"]), 2)
        self.assertEqual(list_failed_jobs(limit_per_kind=10_000)["limit_per_kind"], 100)

    def test_every_registered_kind_appears_in_the_result(self):
        result = list_failed_jobs()
        self.assertIn("enhancement", result["kinds"])
        self.assertIn("upload_batch", result["kinds"])
        self.assertIn("identity_analysis", result["kinds"])


class FailedJobsEndpointTests(AdminManagementFixtureMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("failed-jobs-admin", role="admin")
        self.plain = self.make_user("failed-jobs-plain")
        self.client = APIClient()
        self.url = reverse("operations:failed-jobs")

    def test_requires_admin(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.plain)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_sees_the_payload(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("kinds", response.data)

    def test_bad_kind_query_param_is_400(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url, {"kind": "nonsense"})
        self.assertEqual(response.status_code, 400)


class FailedBatchListEndpointTests(AdminManagementFixtureMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("batches-admin", role="admin")
        self.owner = self.make_user("batches-owner")
        self.client = APIClient()
        self.url = reverse("operations:failed-batches")

    def test_requires_admin(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_lists_only_failed_batches_with_owner_info(self):
        failed = UploadBatch.objects.create(owner=self.owner, upload_type="single", processing_status="failed")
        UploadBatch.objects.create(owner=self.owner, upload_type="single", processing_status="completed")
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids, [failed.pk])
        self.assertEqual(response.data["results"][0]["owner_username"], self.owner.username)

    def test_is_paginated(self):
        for _ in range(3):
            UploadBatch.objects.create(owner=self.owner, upload_type="single", processing_status="failed")
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url, {"page_size": 2})

        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])


class AdminBatchRetryEndpointTests(AdminManagementFixtureMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("retry-admin", role="admin")
        self.plain = self.make_user("retry-plain")
        self.owner = self.make_user("retry-owner")
        self.client = APIClient()

    def make_failed_batch(self, owner):
        """A batch with one image whose preprocessing failed.

        Creating the image and its ImagePreprocessingResult already triggers
        uploads.signals to create/update its BatchImageProgress row, so this
        only has to point that row at the failure (not create a second one,
        which would violate the one-progress-row-per-image constraint).
        """
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = self.make_image(owner, batch)
        ImagePreprocessingResult.objects.create(
            owner=owner, image=image, status="failed", failure_reason="decoder failed",
        )
        BatchImageProgress.objects.filter(image=image).update(
            status="failed", failure_stage="preprocessing", failure_reason="decoder failed",
        )
        return batch

    def url(self, batch_id):
        return reverse("operations:admin-batch-retry", kwargs={"batch_id": batch_id})

    def test_requires_admin(self):
        batch = self.make_failed_batch(self.owner)
        self.assertEqual(self.client.post(self.url(batch.pk)).status_code, 401)
        self.client.force_authenticate(self.plain)
        self.assertEqual(self.client.post(self.url(batch.pk)).status_code, 403)

    @patch("uploads.services.enqueue_batch_preprocessing")
    def test_admin_can_retry_another_users_batch(self, enqueue):
        batch = self.make_failed_batch(self.owner)
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url(batch.pk))

        self.assertEqual(response.status_code, 202)
        batch.refresh_from_db()
        self.assertEqual(batch.retry_count, 1)
        enqueue.assert_called_once()

    def test_a_batch_with_nothing_failed_is_400(self):
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url(batch.pk))

        self.assertEqual(response.status_code, 400)

    def test_an_unknown_batch_is_404(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url(999999))
        self.assertEqual(response.status_code, 404)

    @override_settings(BATCH_PROGRESS_MAX_RETRIES=1)
    def test_the_retry_cap_still_applies_to_admin_retries(self):
        batch = self.make_failed_batch(self.owner)
        UploadBatch.objects.filter(pk=batch.pk).update(retry_count=1)
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url(batch.pk))

        self.assertEqual(response.status_code, 400)

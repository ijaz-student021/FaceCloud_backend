from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import EnhancementBatchJob, ImageEnhancementResult
from .services import TooManyActiveJobs, ensure_enhancement_capacity

User = get_user_model()


class EnhancementCapacityTests(TestCase):
    def make_user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )

    def make_image(self, user, batch, name):
        image = ImageUpload.objects.create(
            owner=user, batch=batch, original_name=name, status="uploaded",
        )
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=user, image=image, status="completed", output_format="png",
        )
        return image, preprocessing

    def setUp(self):
        self.user = self.make_user("capacity-owner")
        self.other = self.make_user("capacity-other")
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def start_single(self, image, preprocessing, status="processing"):
        return ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=preprocessing, status=status,
        )

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1)
    def test_single_image_requests_over_the_limit_get_429(self):
        busy_image, busy_pre = self.make_image(self.user, self.batch, "busy.png")
        new_image, _ = self.make_image(self.user, self.batch, "new.png")
        self.start_single(busy_image, busy_pre)

        response = self.client.post(
            reverse("enhancement:image-enhance", kwargs={"image_id": new_image.pk}), {}, format="json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("limit 1", response.data["detail"])
        self.assertFalse(ImageEnhancementResult.objects.filter(image=new_image).exists())

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1)
    def test_finished_work_and_other_users_do_not_count(self):
        image, pre = self.make_image(self.user, self.batch, "done.png")
        self.start_single(image, pre, status="completed")
        ensure_enhancement_capacity(self.user, batch=False)

        other_batch = UploadBatch.objects.create(owner=self.other, upload_type="single")
        other_image, other_pre = self.make_image(self.other, other_batch, "other.png")
        ImageEnhancementResult.objects.create(
            owner=self.other, image=other_image, preprocessing_result=other_pre,
            status="processing",
        )
        ensure_enhancement_capacity(self.user, batch=False)

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1)
    def test_results_inside_a_batch_do_not_use_the_single_image_allowance(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="processing", total_images=3,
        )
        for index in range(3):
            image, pre = self.make_image(self.user, self.batch, f"b{index}.png")
            ImageEnhancementResult.objects.create(
                owner=self.user, image=image, preprocessing_result=pre,
                batch_job=job, status="processing",
            )

        ensure_enhancement_capacity(self.user, batch=False)

    @override_settings(MAX_ACTIVE_BATCH_ENHANCEMENTS_PER_USER=1)
    def test_batch_requests_over_the_limit_get_429(self):
        EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="queued",
        )

        response = self.client.post(
            reverse("enhancement:batch-enhance", kwargs={"batch_id": self.batch.pk}), {}, format="json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("batch enhancement jobs", response.data["detail"])
        self.assertEqual(EnhancementBatchJob.objects.count(), 1)

    @override_settings(MAX_ACTIVE_BATCH_ENHANCEMENTS_PER_USER=1)
    def test_a_finished_batch_frees_the_slot(self):
        EnhancementBatchJob.objects.create(
            owner=self.user, source_batch=self.batch, status="completed",
        )
        _, preprocessing = self.make_image(self.user, self.batch, "ready.png")
        # Enqueueing only needs a recorded output; the task itself is mocked.
        ImagePreprocessingResult.objects.filter(pk=preprocessing.pk).update(
            processed_file="users/1/preprocessed/ready.png",
        )

        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(
                reverse("enhancement:batch-enhance", kwargs={"batch_id": self.batch.pk}), {}, format="json",
            )

        self.assertEqual(response.status_code, 201)

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=0)
    def test_a_limit_of_zero_disables_the_check(self):
        for index in range(3):
            image, pre = self.make_image(self.user, self.batch, f"s{index}.png")
            self.start_single(image, pre)

        ensure_enhancement_capacity(self.user, batch=False)

    @override_settings(MAX_ACTIVE_ENHANCEMENTS_PER_USER=1)
    def test_the_error_carries_a_machine_readable_code(self):
        image, pre = self.make_image(self.user, self.batch, "x.png")
        self.start_single(image, pre)

        with self.assertRaises(TooManyActiveJobs) as caught:
            ensure_enhancement_capacity(self.user, batch=False)

        self.assertEqual(caught.exception.get_codes(), "too_many_active_jobs")
        self.assertEqual(caught.exception.status_code, 429)

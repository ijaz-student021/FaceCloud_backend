"""Photos inside batch responses and progress rows carry the same display_status as My Images."""
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APITestCase

from quality_control.models import ImageQualityDecision

from . import tests as upload_tests
from .models import ImageUpload, UploadBatch

User = get_user_model()


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class BatchDisplayStatusTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(
            MEDIA_ROOT=self.media.name, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True,
        )
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(
            username="bds", email="bds@example.com", password="OrchidRiver!4729", is_email_verified=True,
        )
        self.client.force_authenticate(self.user)

    def upload(self, count=2, with_bad_file=False):
        files = [upload_tests.make_image(f"p{index}.jpg") for index in range(count)]
        if with_bad_file:
            files.append(SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"))
        response = self.client.post(reverse("image-upload-multiple"), {"files": files}, format="multipart")
        self.assertEqual(response.status_code, 201)
        return response.json()

    def my_images_status(self):
        rows = self.client.get(reverse("my-image-list"), {"page_size": 100}).json()["results"]
        return {row["id"]: row["display_status"] for row in rows}

    def test_the_upload_response_gives_every_photo_a_display_status(self):
        body = self.upload(with_bad_file=True)
        by_name = {image["original_name"]: image for image in body["images"]}
        self.assertEqual(by_name["notes.txt"]["display_status"], "rejected")
        self.assertEqual(by_name["p0.jpg"]["display_status"], self.my_images_status()[by_name["p0.jpg"]["id"]])
        for image in body["images"]:
            self.assertIn("display_status", image)
            for key in ("id", "original_name", "status", "rejection_reason", "thumbnail_url"):
                self.assertIn(key, image)

    def test_batch_list_and_detail_agree_with_my_images(self):
        body = self.upload()
        expected = self.my_images_status()
        detail = self.client.get(reverse("upload-batch-detail", args=[body["id"]])).json()
        listed = self.client.get(reverse("upload-batch-list")).json()
        listed = listed["results"] if isinstance(listed, dict) else listed
        for batch in (detail, listed[0]):
            for image in batch["images"]:
                self.assertEqual(image["display_status"], expected[image["id"]])

    def test_progress_rows_carry_display_status_too(self):
        body = self.upload()
        expected = self.my_images_status()
        progress = self.client.get(reverse("upload-batch-progress", args=[body["id"]])).json()
        self.assertEqual(len(progress["images"]), 2)
        for row in progress["images"]:
            self.assertEqual(row["display_status"], expected[row["image_id"]])
            self.assertIn("status", row)
            self.assertIn("stage", row)

    def test_a_quality_verdict_shows_up_in_the_status(self):
        body = self.upload()
        image = ImageUpload.objects.get(pk=body["images"][0]["id"])
        ImageQualityDecision.objects.update_or_create(
            image=image, defaults={
                "owner": self.user, "preprocessing_result": image.preprocessing_result, "status": "completed",
                "decision": "rejected", "overall_score": 20, "brightness_score": 20, "sharpness_score": 20,
            },
        )
        progress = self.client.get(reverse("upload-batch-progress", args=[body["id"]])).json()
        statuses = {row["image_id"]: row["display_status"] for row in progress["images"]}
        self.assertEqual(statuses[image.pk], "rejected")
        detail = self.client.get(reverse("upload-batch-detail", args=[body["id"]])).json()
        self.assertEqual({i["id"]: i["display_status"] for i in detail["images"]}[image.pk], "rejected")

    def test_the_number_of_queries_does_not_grow_with_the_number_of_photos(self):
        small = self.upload(count=2)
        large = self.upload(count=7)
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("upload-batch-detail", args=[small["id"]]))
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("upload-batch-detail", args=[large["id"]]))
        self.assertEqual(len(few), len(many))

    def test_another_users_batch_is_404(self):
        other = User.objects.create_user(
            username="bds-other", email="bds-other@example.com", password="OrchidRiver!4729", is_email_verified=True,
        )
        theirs = UploadBatch.objects.create(owner=other, upload_type="multiple")
        self.assertEqual(self.client.get(reverse("upload-batch-detail", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("upload-batch-progress", args=[theirs.pk])).status_code, 404)

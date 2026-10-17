"""What the "Summary / upload results" and "Processing Complete" panels read from an upload response.

Verification only: these tests pin the existing behaviour so the panels keep working.
"""
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from . import tests as upload_tests

User = get_user_model()


class BatchResultsContractTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(
            MEDIA_ROOT=self.media.name, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True,
        )
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(
            username="contract", email="contract@example.com", password="OrchidRiver!4729", is_email_verified=True,
        )
        self.client.force_authenticate(self.user)

    def assert_results(self, body, *, total, accepted, rejected, rejected_name):
        self.assertEqual((body["total_files"], body["accepted_files"], body["rejected_files"]), (total, accepted, rejected))
        self.assertEqual(len(body["images"]), total)
        by_name = {image["original_name"]: image for image in body["images"]}
        refused = by_name[rejected_name]
        self.assertEqual(refused["status"], "rejected")
        self.assertTrue(refused["rejection_reason"])
        self.assertIsNone(refused["thumbnail_url"])
        accepted_rows = [image for image in body["images"] if image["status"] == "uploaded"]
        self.assertEqual(len(accepted_rows), accepted)
        for image in accepted_rows:
            self.assertEqual(image["rejection_reason"], "")
            self.assertTrue(image["thumbnail_url"])
        for key in ("processing_status", "processed_files", "processing_failed_files", "status"):
            self.assertIn(key, body)

    def test_multiple_upload_reports_each_files_result_and_the_counts(self):
        response = self.client.post(
            reverse("image-upload-multiple"),
            {"files": [
                upload_tests.make_image("one.jpg"), upload_tests.make_image("two.png", "PNG"),
                SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"),
            ]},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.assert_results(response.json(), total=3, accepted=2, rejected=1, rejected_name="notes.txt")

    def test_zip_upload_reports_each_members_result_and_the_counts(self):
        archive = upload_tests.make_zip([
            ("folder/one.jpg", upload_tests.image_bytes()),
            ("two.png", upload_tests.image_bytes("PNG")),
            ("notes.txt", b"not an image"),
        ])
        response = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["upload_type"], "zip")
        self.assertTrue(body["original_archive_name"])
        self.assert_results(body, total=3, accepted=2, rejected=1, rejected_name="notes.txt")

    def test_the_progress_endpoint_keeps_reporting_the_same_counts(self):
        response = self.client.post(
            reverse("image-upload-multiple"),
            {"files": [upload_tests.make_image("one.jpg"), upload_tests.make_image("two.jpg")]},
            format="multipart",
        )
        progress = self.client.get(reverse("upload-batch-progress", args=[response.json()["id"]]))
        self.assertEqual(progress.status_code, 200)
        self.assertIn("progress_percentage", progress.json())
        self.assertEqual(len(progress.json()["images"]), 2)

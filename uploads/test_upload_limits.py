"""The upload limits the API reports are the ones upload validation enforces."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User


def png(name="photo.png", size=(8, 8)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 80, 40)).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(DUPLICATE_AUTO_ANALYZE=False, IDENTITY_AUTO_ANALYZE=False)
class UploadLimitsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="limits", email="limits@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def limits(self):
        response = self.client.get("/api/images/upload/limits/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_it_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/images/upload/limits/").status_code, 401)

    @override_settings(MAX_UPLOAD_BATCH_FILES=2)
    def test_the_batch_limit_reported_is_the_one_enforced(self):
        self.assertEqual(self.limits()["max_files_per_batch"], 2)
        refused = self.client.post("/api/images/upload/multiple/", {"files": [png(f"{i}.png") for i in range(3)]},
                                   format="multipart")
        self.assertEqual(refused.status_code, 400)

    def test_an_extension_not_listed_is_refused(self):
        self.assertNotIn(".svg", self.limits()["accepted_extensions"])
        upload = SimpleUploadedFile("drawing.svg", b"<svg/>", content_type="image/svg+xml")
        self.assert_rejected(self.client.post("/api/images/upload/single/", {"file": upload}, format="multipart"))

    def assert_rejected(self, response):
        """Single uploads answer 201 with the file recorded as rejected and why."""
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["status"], body["rejected_files"]), ("rejected", 1))
        self.assertEqual(body["images"][0]["status"], "rejected")
        self.assertTrue(body["images"][0]["rejection_reason"])

    def test_a_listed_extension_is_accepted(self):
        self.assertIn(".png", self.limits()["accepted_extensions"])
        accepted = self.client.post("/api/images/upload/single/", {"file": png()}, format="multipart")
        self.assertEqual(accepted.status_code, 201, accepted.content[:300])
        self.assertEqual(accepted.json()["images"][0]["status"], "uploaded")

    @override_settings(MAX_IMAGE_UPLOAD_SIZE=50)
    def test_the_size_limit_reported_is_the_one_enforced(self):
        self.assertEqual(self.limits()["max_image_bytes"], 50)
        self.assert_rejected(self.client.post("/api/images/upload/single/", {"file": png()}, format="multipart"))

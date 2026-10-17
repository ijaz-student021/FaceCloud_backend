"""Profile details and the user's avatar (B02)."""
import io
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from organizations.models import Organization

from .models import User

MEDIA = tempfile.mkdtemp(prefix="facecloud-avatar-test-")


def image_file(fmt="JPEG", size=(1200, 800), name="me.jpg", exif=None):
    buffer = io.BytesIO()
    options = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, (200, 120, 80)).save(buffer, fmt, **options)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f"image/{fmt.lower()}")


@override_settings(MEDIA_ROOT=MEDIA)
class ProfileAndAvatarTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = self.make_user("profile")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def make_user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="ForestRiver!4821", is_email_verified=True,
        )

    def test_profile_details_persist(self):
        response = self.client.patch("/api/user/profile/", {
            "display_name": "  Ali   Hassan ", "organization_name": "Studio North", "job_title": "Photographer",
            "phone": "+92 300 1234567", "country": "Pakistan",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        body = self.client.get("/api/user/profile/").json()
        self.assertEqual(
            [body[key] for key in ("display_name", "organization_name", "job_title", "phone", "country")],
            ["Ali Hassan", "Studio North", "Photographer", "+92 300 1234567", "Pakistan"],
        )

    def test_invalid_details_are_refused(self):
        self.assertEqual(self.client.patch("/api/user/profile/", {"phone": "call me maybe"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/user/profile/", {"job_title": "x" * 200}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/user/profile/", {"country": "bad\x07"}, format="json").status_code, 400)

    def test_organization_text_grants_no_organization_access(self):
        organization = Organization.objects.create(name="Studio North", slug="studio-north")
        self.client.patch("/api/user/profile/", {"organization_name": "Studio North"}, format="json")
        self.assertEqual(self.client.get(f"/api/organizations/{organization.pk}/").status_code, 404)

    def test_login_responses_carry_the_profile(self):
        self.client.patch("/api/user/profile/", {"display_name": "Ali"}, format="json")
        login = APIClient().post("/api/auth/login/", {"email": "profile@example.com", "password": "ForestRiver!4821"},
                                 format="json")
        self.assertEqual(login.status_code, 200, login.content)
        self.assertEqual(login.json()["user"]["display_name"], "Ali")

    def test_an_avatar_is_re_encoded_small_and_without_metadata(self):
        exif = Image.Exif()
        exif[0x8825] = {2: (33.0, 41.0, 0.0)}  # a GPS block
        response = self.client.put("/api/user/avatar/", {"avatar": image_file(exif=exif)}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["avatar_url"].startswith("/api/user/avatar/?v="))

        served = self.client.get("/api/user/avatar/")
        self.assertEqual((served.status_code, served["Content-Type"]), (200, "image/png"))
        image = Image.open(io.BytesIO(b"".join(served.streaming_content)))
        self.assertEqual((image.format, max(image.size)), ("PNG", 512))
        self.assertFalse(image.getexif())

    def test_replacing_an_avatar_removes_the_old_file(self):
        self.client.put("/api/user/avatar/", {"avatar": image_file()}, format="multipart")
        first = User.objects.get(pk=self.user.pk).avatar.name
        self.client.put("/api/user/avatar/", {"avatar": image_file("PNG", name="me.png")}, format="multipart")
        second = User.objects.get(pk=self.user.pk).avatar.name
        self.assertNotEqual(first, second)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(second))

    def test_removing_an_avatar(self):
        self.client.put("/api/user/avatar/", {"avatar": image_file()}, format="multipart")
        name = User.objects.get(pk=self.user.pk).avatar.name
        self.assertEqual(self.client.delete("/api/user/avatar/").status_code, 204)
        self.assertFalse(default_storage.exists(name))
        self.assertEqual(self.client.get("/api/user/avatar/").status_code, 404)
        self.assertIsNone(self.client.get("/api/user/profile/").json()["avatar_url"])

    def test_bad_uploads_are_refused(self):
        not_image = SimpleUploadedFile("me.jpg", b"not an image", content_type="image/jpeg")
        self.assertEqual(self.client.put("/api/user/avatar/", {"avatar": not_image}, format="multipart").status_code, 400)
        gif = image_file("GIF", name="me.gif")
        self.assertEqual(self.client.put("/api/user/avatar/", {"avatar": gif}, format="multipart").status_code, 400)
        with patch("accounts.avatars.AVATAR_MAX_BYTES", 100):
            too_big = self.client.put("/api/user/avatar/", {"avatar": image_file()}, format="multipart")
        self.assertEqual(too_big.status_code, 400)

    def test_an_avatar_is_only_ever_served_to_its_owner(self):
        self.client.put("/api/user/avatar/", {"avatar": image_file()}, format="multipart")
        other = APIClient()
        other.force_authenticate(self.make_user("other"))
        self.assertEqual(other.get("/api/user/avatar/").status_code, 404)  # their own, which they lack
        self.assertEqual(APIClient().get("/api/user/avatar/").status_code, 401)

    def test_deleting_the_account_deletes_the_avatar_file(self):
        self.client.put("/api/user/avatar/", {"avatar": image_file()}, format="multipart")
        name = User.objects.get(pk=self.user.pk).avatar.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete("/api/user/delete-account/", {"password": "ForestRiver!4821"}, format="json")
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(default_storage.exists(name))

    def test_the_privacy_export_includes_the_profile(self):
        self.client.patch("/api/user/profile/", {"phone": "+44 20 7946 0000", "job_title": "Editor"}, format="json")
        export = self.client.get("/api/privacy/export/")
        self.assertEqual(export.status_code, 200)
        body = export.json()
        profile = body["account"]["profile"]
        self.assertEqual((profile["phone"], profile["job_title"], profile["has_avatar"]), ("+44 20 7946 0000", "Editor", False))

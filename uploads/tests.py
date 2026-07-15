import io
import os
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import ImageUpload, UploadBatch

User = get_user_model()


def make_image(name="photo.jpg", image_format="JPEG", size=(32, 24), color="blue"):
    content = io.BytesIO()
    Image.new("RGB", size, color=color).save(content, format=image_format)
    mime = "image/png" if image_format == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, content.getvalue(), content_type=mime)


def make_zip(entries, name="images.zip"):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in entries:
            archive.writestr(filename, data)
    return SimpleUploadedFile(name, content.getvalue(), content_type="application/zip")


def image_bytes(image_format="JPEG"):
    return make_image("temporary.jpg", image_format).read()


class ImageUploadTests(APITestCase):
    password = "OrchidRiver!4729"

    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.settings_override.enable()
        self.user = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password=self.password,
            is_email_verified=True,
        )
        self.authenticate(self.user)

    def tearDown(self):
        self.settings_override.disable()
        self.media_directory.cleanup()

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_valid_single_image_upload_stores_secure_metadata(self):
        response = self.client.post(
            reverse("image-upload-single"),
            {"file": make_image("My Profile.JPG")},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["accepted_files"], 1)
        image = ImageUpload.objects.get()
        self.assertEqual(image.original_name, "My Profile.JPG")
        self.assertEqual(image.mime_type, "image/jpeg")
        self.assertEqual((image.width, image.height), (32, 24))
        self.assertEqual(len(image.checksum_sha256), 64)
        self.assertNotIn("My Profile", image.original_file.name)
        self.assertTrue(image.original_file.storage.exists(image.original_file.name))

    def test_invalid_content_is_rejected_without_stored_file(self):
        fake = SimpleUploadedFile("fake.jpg", b"not an image", content_type="image/jpeg")
        response = self.client.post(reverse("image-upload-single"), {"file": fake}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["rejected_files"], 1)
        image = ImageUpload.objects.get()
        self.assertEqual(image.status, "rejected")
        self.assertFalse(bool(image.original_file))
        self.assertIn("valid or readable", image.rejection_reason)

    @override_settings(MAX_IMAGE_UPLOAD_SIZE=10)
    def test_oversized_image_is_rejected(self):
        response = self.client.post(
            reverse("image-upload-single"),
            {"file": make_image()},
            format="multipart",
        )
        self.assertEqual(response.data["rejected_files"], 1)
        self.assertIn("upload limit", response.data["images"][0]["rejection_reason"])

    @override_settings(MAX_IMAGE_PIXELS=10)
    def test_excessive_pixel_count_is_rejected(self):
        response = self.client.post(
            reverse("image-upload-single"),
            {"file": make_image(size=(4, 4))},
            format="multipart",
        )
        self.assertEqual(response.data["rejected_files"], 1)
        self.assertIn("pixel limit", response.data["images"][0]["rejection_reason"])

    def test_multiple_upload_keeps_valid_files_and_rejects_invalid_files(self):
        response = self.client.post(
            reverse("image-upload-multiple"),
            {
                "files": [
                    make_image("one.jpg"),
                    make_image("two.png", "PNG"),
                    SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"),
                ]
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "partial")
        self.assertEqual(response.data["accepted_files"], 2)
        self.assertEqual(response.data["rejected_files"], 1)

    @override_settings(MAX_UPLOAD_BATCH_FILES=2)
    def test_multiple_upload_enforces_batch_count(self):
        response = self.client.post(
            reverse("image-upload-multiple"),
            {"files": [make_image("1.jpg"), make_image("2.jpg"), make_image("3.jpg")]},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(UploadBatch.objects.count(), 0)

    def test_zip_upload_accepts_images_and_records_unsupported_files(self):
        archive = make_zip(
            [
                ("folder/one.jpg", image_bytes()),
                ("two.png", image_bytes("PNG")),
                ("notes.txt", b"not an image"),
            ]
        )
        response = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["accepted_files"], 2)
        self.assertEqual(response.data["rejected_files"], 1)
        self.assertEqual(response.data["original_archive_name"], "images.zip")

    def test_zip_path_traversal_is_rejected(self):
        archive = make_zip([("../escape.jpg", image_bytes())])
        response = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(UploadBatch.objects.count(), 0)

    def test_nested_zip_is_rejected(self):
        archive = make_zip([("nested.zip", b"anything")])
        response = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_zip_is_rejected(self):
        archive = SimpleUploadedFile("images.zip", b"not a zip", content_type="application/zip")
        response = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_and_unverified_users_cannot_upload(self):
        self.client.credentials()
        unauthenticated = self.client.post(
            reverse("image-upload-single"), {"file": make_image()}, format="multipart"
        )
        self.assertEqual(unauthenticated.status_code, status.HTTP_401_UNAUTHORIZED)
        unverified = User.objects.create_user(
            username="unverified", email="unverified@example.com", password=self.password
        )
        self.authenticate(unverified)
        denied = self.client.post(
            reverse("image-upload-single"), {"file": make_image()}, format="multipart"
        )
        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_users_cannot_list_view_download_or_delete_another_users_image(self):
        uploaded = self.client.post(
            reverse("image-upload-single"), {"file": make_image()}, format="multipart"
        )
        image_id = uploaded.data["images"][0]["id"]
        other = User.objects.create_user(
            username="other", email="other@example.com", password=self.password, is_email_verified=True
        )
        self.authenticate(other)
        listed = self.client.get(reverse("image-upload-list"))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listed.data), 0)
        self.assertEqual(self.client.get(reverse("image-upload-detail", kwargs={"pk": image_id})).status_code, 404)
        self.assertEqual(self.client.get(reverse("image-upload-download", kwargs={"pk": image_id})).status_code, 404)
        self.assertEqual(self.client.delete(reverse("image-upload-detail", kwargs={"pk": image_id})).status_code, 404)

    def test_image_delete_removes_database_record_and_stored_file(self):
        uploaded = self.client.post(
            reverse("image-upload-single"), {"file": make_image()}, format="multipart"
        )
        image = ImageUpload.objects.get(pk=uploaded.data["images"][0]["id"])
        stored_path = image.original_file.path
        self.assertTrue(os.path.exists(stored_path))
        response = self.client.delete(reverse("image-upload-detail", kwargs={"pk": image.pk}))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ImageUpload.objects.filter(pk=image.pk).exists())
        self.assertFalse(os.path.exists(stored_path))

    def test_batch_delete_removes_all_stored_files(self):
        uploaded = self.client.post(
            reverse("image-upload-multiple"),
            {"files": [make_image("one.jpg"), make_image("two.jpg")]},
            format="multipart",
        )
        batch = UploadBatch.objects.get(pk=uploaded.data["id"])
        paths = [image.original_file.path for image in batch.images.all()]
        response = self.client.delete(reverse("upload-batch-detail", kwargs={"pk": batch.pk}))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(UploadBatch.objects.filter(pk=batch.pk).exists())
        self.assertTrue(all(not os.path.exists(path) for path in paths))

    def test_account_deletion_cascade_removes_stored_files(self):
        uploaded = self.client.post(
            reverse("image-upload-single"), {"file": make_image()}, format="multipart"
        )
        image = ImageUpload.objects.get(pk=uploaded.data["images"][0]["id"])
        stored_path = image.original_file.path
        self.user.delete()
        self.assertFalse(os.path.exists(stored_path))

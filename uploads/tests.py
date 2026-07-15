import io
import os
import tempfile
import zipfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch

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


def make_custom_image(name, image_format, mode="RGB", size=(32, 24), color="blue", exif=None):
    content = io.BytesIO()
    image = Image.new(mode, size, color=color)
    options = {"exif": exif} if exif else {}
    image.save(content, format=image_format, **options)
    mime_types = {
        "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
        "BMP": "image/bmp", "TIFF": "image/tiff", "GIF": "image/gif",
    }
    return SimpleUploadedFile(name, content.getvalue(), content_type=mime_types[image_format])


class ImageUploadTests(APITestCase):
    password = "OrchidRiver!4729"

    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
            CELERY_TASK_ALWAYS_EAGER=True,
            CELERY_TASK_EAGER_PROPAGATES=True,
        )
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
        result = ImagePreprocessingResult.objects.get(image=image)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.requested_format, "auto")

    def test_single_upload_rejects_zero_or_multiple_files(self):
        missing = self.client.post(reverse("image-upload-single"), {}, format="multipart")
        multiple = self.client.post(
            reverse("image-upload-single"),
            {"file": [make_image("one.jpg"), make_image("two.jpg")]},
            format="multipart",
        )
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(multiple.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly one", str(multiple.data["file"]).lower())

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
        results = ImagePreprocessingResult.objects.filter(image__batch_id=response.data["id"])
        self.assertEqual(results.count(), 2)
        self.assertFalse(results.exclude(status="completed").exists())

    def test_all_upload_modes_return_automatic_preprocessing_results(self):
        single = self.client.post(
            reverse("image-upload-single"),
            {"file": make_image("single.png", "PNG")},
            format="multipart",
        )
        multiple = self.client.post(
            reverse("image-upload-multiple"),
            {"files": [make_image("one.jpg"), make_image("two.png", "PNG")]},
            format="multipart",
        )
        archive = make_zip([("zip.webp", make_custom_image("zip.webp", "WEBP").read())])
        zipped = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")

        for response in (single, multiple, zipped):
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            for image_data in response.data["images"]:
                if image_data["status"] == "uploaded":
                    self.assertEqual(image_data["preprocessing"]["status"], "completed")
                    self.assertIn(image_data["preprocessing"]["output_format"], {"png", "jpeg"})

    def test_multiple_upload_requires_at_least_two_images(self):
        response = self.client.post(
            reverse("image-upload-multiple"),
            {"files": [make_image("only-one.jpg")]},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at least two", str(response.data["files"]).lower())

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

    def test_zip_upload_automatically_preprocesses_webp_and_png_images(self):
        webp = make_custom_image(
            "transparent.webp", "WEBP", mode="RGBA", color=(255, 0, 0, 0)
        ).read()
        png = make_custom_image("photo.png", "PNG").read()
        archive = make_zip([("web/transparent.webp", webp), ("png/photo.png", png)])

        response = self.client.post(reverse("image-upload-zip"), {"file": archive}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["accepted_files"], 2)
        self.assertEqual(response.data["rejected_files"], 0)
        results = ImagePreprocessingResult.objects.filter(image__batch_id=response.data["id"])
        self.assertEqual(results.count(), 2)
        self.assertFalse(results.exclude(status="completed").exists())
        self.assertEqual(results.get(image__original_name="transparent.webp").output_format, "png")
        self.assertEqual(results.get(image__original_name="photo.png").output_format, "png")

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

    def test_zip_upload_requires_exactly_one_archive(self):
        missing = self.client.post(reverse("image-upload-zip"), {}, format="multipart")
        multiple = self.client.post(
            reverse("image-upload-zip"),
            {"file": [make_zip([("one.jpg", image_bytes())], "one.zip"), make_zip([("two.jpg", image_bytes())], "two.zip")]},
            format="multipart",
        )
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(multiple.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly one", str(multiple.data["file"]).lower())

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

    def upload_image(self, uploaded_file):
        response = self.client.post(reverse("image-upload-single"), {"file": uploaded_file}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["accepted_files"], 1)
        return ImageUpload.objects.get(pk=response.data["images"][0]["id"])

    def preprocess(self, image, **payload):
        return self.client.post(reverse("image-preprocess", kwargs={"pk": image.pk}), payload, format="json")

    def test_upload_accepts_webp_bmp_tiff_and_static_gif(self):
        cases = [
            ("photo.webp", "WEBP"),
            ("photo.bmp", "BMP"),
            ("photo.tiff", "TIFF"),
            ("photo.gif", "GIF"),
        ]
        for name, image_format in cases:
            with self.subTest(image_format=image_format):
                response = self.client.post(
                    reverse("image-upload-single"),
                    {"file": make_custom_image(name, image_format)},
                    format="multipart",
                )
                self.assertEqual(response.data["accepted_files"], 1)

    def test_animated_gif_is_rejected(self):
        content = io.BytesIO()
        frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
        frames[0].save(content, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
        uploaded = SimpleUploadedFile("animated.gif", content.getvalue(), content_type="image/gif")
        response = self.client.post(reverse("image-upload-single"), {"file": uploaded}, format="multipart")
        self.assertEqual(response.data["rejected_files"], 1)
        self.assertIn("Animated GIF", response.data["images"][0]["rejection_reason"])

    def test_webp_is_converted_to_requested_jpeg_and_original_is_preserved(self):
        image = self.upload_image(make_custom_image("source.webp", "WEBP", size=(40, 30)))
        original_checksum = image.checksum_sha256
        original_path = image.original_file.path
        response = self.preprocess(image, output_format="jpeg", output_name="Profile Photo")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["output_format"], "jpeg")
        self.assertEqual(response.data["output_name"], "Profile Photo.jpg")
        result = ImagePreprocessingResult.objects.get(image=image)
        with Image.open(result.processed_file.path) as processed:
            self.assertEqual(processed.format, "JPEG")
        image.refresh_from_db()
        self.assertEqual(image.checksum_sha256, original_checksum)
        self.assertTrue(os.path.exists(original_path))

    def test_bmp_and_tiff_convert_to_png(self):
        for name, image_format in (("source.bmp", "BMP"), ("source.tif", "TIFF")):
            with self.subTest(image_format=image_format):
                image = self.upload_image(make_custom_image(name, image_format))
                response = self.preprocess(image, output_format="png")
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(response.data["output_format"], "png")

    def test_auto_format_preserves_transparency_as_png(self):
        image = self.upload_image(
            make_custom_image("transparent.png", "PNG", mode="RGBA", color=(255, 0, 0, 0))
        )
        response = self.preprocess(image, output_format="auto")
        self.assertEqual(response.data["output_format"], "png")
        self.assertTrue(response.data["had_transparency"])

    def test_auto_format_preserves_opaque_png_as_png(self):
        image = self.upload_image(make_custom_image("text-screenshot.png", "PNG"))
        result = ImagePreprocessingResult.objects.get(image=image)
        self.assertEqual(result.requested_format, "auto")
        self.assertEqual(result.output_format, "png")

    def test_transparent_png_to_jpeg_uses_configured_background(self):
        image = self.upload_image(
            make_custom_image("transparent.png", "PNG", mode="RGBA", size=(8, 8), color=(255, 0, 0, 0))
        )
        response = self.preprocess(image, output_format="jpeg")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        result = ImagePreprocessingResult.objects.get(image=image)
        with Image.open(result.processed_file.path) as processed:
            pixel = processed.convert("RGB").getpixel((4, 4))
            self.assertTrue(all(channel > 240 for channel in pixel))

    @override_settings(PREPROCESS_MAX_WIDTH=20, PREPROCESS_MAX_HEIGHT=20)
    def test_preprocessing_resizes_without_upscaling_and_preserves_aspect_ratio(self):
        large = self.upload_image(make_image("large.jpg", size=(40, 20)))
        response = self.preprocess(large, output_format="jpeg")
        self.assertEqual((response.data["output_width"], response.data["output_height"]), (20, 10))
        small = self.upload_image(make_image("small.jpg", size=(10, 5)))
        response = self.preprocess(small, output_format="jpeg")
        self.assertEqual((response.data["output_width"], response.data["output_height"]), (10, 5))

    def test_exif_orientation_is_corrected(self):
        exif = Image.Exif()
        exif[274] = 6
        image = self.upload_image(make_custom_image("rotated.jpg", "JPEG", size=(30, 20), exif=exif))
        response = self.preprocess(image, output_format="jpeg")
        self.assertEqual((response.data["output_width"], response.data["output_height"]), (20, 30))

    def test_safe_output_naming_and_idempotent_reuse(self):
        image = self.upload_image(make_image())
        first = self.preprocess(image, output_format="png", output_name="../unsafe\\profile?.jpg")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["output_name"], "profile.png")
        original_stored_name = ImagePreprocessingResult.objects.get(image=image).processed_file.name
        second = self.preprocess(image, output_format="png", output_name="../unsafe\\profile?.jpg")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(ImagePreprocessingResult.objects.get(image=image).processed_file.name, original_stored_name)

    def test_force_reprocessing_replaces_old_processed_file(self):
        image = self.upload_image(make_image())
        self.preprocess(image, output_format="jpeg")
        result = ImagePreprocessingResult.objects.get(image=image)
        old_path = result.processed_file.path
        response = self.preprocess(image, output_format="jpeg", force=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        result.refresh_from_db()
        self.assertNotEqual(result.processed_file.path, old_path)
        self.assertFalse(os.path.exists(old_path))

    def test_preprocessing_status_download_and_cross_user_access(self):
        image = self.upload_image(make_image())
        self.preprocess(image, output_format="png", output_name="download")
        detail = self.client.get(reverse("image-preprocessing-detail", kwargs={"pk": image.pk}))
        download = self.client.get(reverse("image-preprocessing-download", kwargs={"pk": image.pk}))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(download.status_code, status.HTTP_200_OK)
        for closer in list(download._resource_closers):
            closer()
        download._resource_closers.clear()
        other = User.objects.create_user(
            username="preprocess-other", email="preprocess-other@example.com",
            password=self.password, is_email_verified=True,
        )
        self.authenticate(other)
        self.assertEqual(self.client.post(reverse("image-preprocess", kwargs={"pk": image.pk}), {}, format="json").status_code, 404)
        self.assertEqual(self.client.get(reverse("image-preprocessing-detail", kwargs={"pk": image.pk})).status_code, 404)
        self.assertEqual(self.client.get(reverse("image-preprocessing-download", kwargs={"pk": image.pk})).status_code, 404)

    def test_batch_display_name_can_be_renamed_by_owner_only(self):
        image = self.upload_image(make_image())
        response = self.client.patch(
            reverse("upload-batch-detail", kwargs={"pk": image.batch_id}),
            {"display_name": "  Ali   Wedding Photos  "},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["display_name"], "Ali Wedding Photos")

    def test_deleting_image_removes_processed_file(self):
        image = self.upload_image(make_image())
        self.preprocess(image, output_format="png")
        result = ImagePreprocessingResult.objects.get(image=image)
        processed_path = result.processed_file.path
        self.client.delete(reverse("image-upload-detail", kwargs={"pk": image.pk}))
        self.assertFalse(os.path.exists(processed_path))

    def test_multiple_batch_reports_background_processing_failure(self):
        with patch("uploads.tasks.preprocess_image", side_effect=ValidationError("worker failure")):
            response = self.client.post(
                reverse("image-upload-multiple"),
                {"files": [make_image("one.jpg"), make_image("two.jpg")]},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["processing_status"], "failed")
        self.assertEqual(response.data["processed_files"], 0)
        self.assertEqual(response.data["processing_failed_files"], 2)
        self.assertTrue(response.data["processing_error"])

    def test_batch_reports_queue_unavailable_without_losing_originals(self):
        with patch("uploads.tasks.preprocess_batch_task.apply_async", side_effect=OSError("redis unavailable")):
            response = self.client.post(
                reverse("image-upload-multiple"),
                {"files": [make_image("one.jpg"), make_image("two.jpg")]},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["accepted_files"], 2)
        self.assertEqual(response.data["processing_status"], "failed")
        self.assertIn("queue", response.data["processing_error"].lower())

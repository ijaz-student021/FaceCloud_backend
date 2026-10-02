import io
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from uploads.models import ImageUpload, UploadBatch

from .models import ImageThumbnail
from .services import ThumbnailUnavailable, get_or_create_thumbnail, render_thumbnail

User = get_user_model()


def encode(image, format_name="PNG", **options):
    buffer = io.BytesIO()
    image.save(buffer, format=format_name, **options)
    return buffer.getvalue()


def noisy_rgb(width, height, seed=1):
    pixels = np.random.default_rng(seed).integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    return Image.fromarray(pixels, "RGB")


class ThumbnailTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="thumb-owner", email="thumb-owner@example.com", password="pass-12345")
        self.other = User.objects.create_user(username="thumb-other", email="thumb-other@example.com", password="pass-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def photo(self, payload, *, owner=None, name="p.png", status="uploaded", store=True):
        owner = owner or self.owner
        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=name, file_extension=Path(name).suffix,
            mime_type="image/png", status=status,
        )
        if store:
            image.original_file.save(name, ContentFile(payload))
        return image

    def url(self, image, **query):
        return reverse("image-thumbnail", kwargs={"pk": image.pk}) + (
            "?" + "&".join(f"{key}={value}" for key, value in query.items()) if query else ""
        )

    def fetch(self, image, **query):
        response = self.client.get(self.url(image, **query))
        body = b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content
        return response, body


class RenderingTests(ThumbnailTestCase):
    def render(self, payload, size=256, name="p.png"):
        image = self.photo(payload, name=name)
        return render_thumbnail(image.original_file, size)

    def test_a_large_photo_is_scaled_to_fit_and_keeps_its_shape(self):
        data, width, height = self.render(encode(noisy_rgb(1200, 600)), 256)
        self.assertEqual((width, height), (256, 128))
        decoded = Image.open(io.BytesIO(data))
        self.assertEqual(decoded.format, "JPEG")
        self.assertEqual(decoded.size, (256, 128))

    def test_a_small_photo_is_never_enlarged(self):
        _data, width, height = self.render(encode(noisy_rgb(100, 60)), 256)
        self.assertEqual((width, height), (100, 60))

    def test_a_tall_photo_is_limited_by_its_height(self):
        _data, width, height = self.render(encode(noisy_rgb(300, 1500)), 256)
        self.assertEqual((width, height), (51, 256))

    def test_exif_orientation_is_applied(self):
        source = noisy_rgb(400, 200)
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise to display
        payload = encode(source, "JPEG", exif=exif)
        _data, width, height = self.render(payload, 256, name="p.jpg")
        self.assertEqual((width, height), (128, 256))

    def test_transparency_is_flattened_onto_white(self):
        transparent = Image.new("RGBA", (200, 200), (255, 0, 0, 0))
        data, _width, _height = self.render(encode(transparent), 128)
        pixel = Image.open(io.BytesIO(data)).getpixel((10, 10))
        self.assertGreater(min(pixel), 240)

    def test_palette_and_grayscale_images_work(self):
        gray = Image.fromarray(np.full((120, 90), 100, dtype=np.uint8), "L")
        palette = noisy_rgb(80, 80).convert("P")
        for source in (gray, palette):
            data, _width, _height = self.render(encode(source), 64)
            self.assertEqual(Image.open(io.BytesIO(data)).mode, "RGB")

    def test_an_animated_gif_uses_its_first_frame(self):
        frames = [Image.new("RGB", (120, 80), color) for color in ("red", "blue")]
        payload = encode(frames[0], "GIF", save_all=True, append_images=frames[1:])
        data, width, height = self.render(payload, 64, name="p.gif")
        self.assertEqual((width, height), (64, 43))
        self.assertEqual(Image.open(io.BytesIO(data)).format, "JPEG")

    def test_garbage_bytes_are_reported_as_unavailable(self):
        image = self.photo(b"this is not an image")
        with self.assertRaises(ThumbnailUnavailable):
            render_thumbnail(image.original_file, 256)


class ThumbnailApiTests(ThumbnailTestCase):
    def test_login_is_required(self):
        image = self.photo(encode(noisy_rgb(400, 300)))
        self.assertEqual(APIClient().get(self.url(image)).status_code, 401)

    def test_it_serves_a_jpeg_of_the_default_size_with_private_caching(self):
        image = self.photo(encode(noisy_rgb(1000, 500)))
        response, body = self.fetch(image)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("private", response["Cache-Control"])
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        self.assertEqual(Image.open(io.BytesIO(body)).size, (256, 128))

    def test_the_larger_size_can_be_requested(self):
        image = self.photo(encode(noisy_rgb(2000, 1000)))
        response, body = self.fetch(image, size=1024)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(body)).size, (1024, 512))

    def test_only_configured_sizes_are_allowed(self):
        image = self.photo(encode(noisy_rgb(400, 300)))
        for value in ("300", "0", "abc", "-5", "99999"):
            response = self.client.get(self.url(image, size=value))
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("size", response.data)
        self.assertFalse(ImageThumbnail.objects.exists())

    def test_it_is_generated_once_and_then_reused(self):
        image = self.photo(encode(noisy_rgb(800, 600)))
        with patch("thumbnails.services.render_thumbnail", wraps=render_thumbnail) as render:
            self.fetch(image)
            self.fetch(image)
            self.fetch(image)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(ImageThumbnail.objects.filter(image=image).count(), 1)
        self.assertEqual(ImageThumbnail.objects.get(image=image).size, 256)

    def test_each_size_is_cached_separately(self):
        image = self.photo(encode(noisy_rgb(2000, 1500)))
        self.fetch(image, size=256)
        self.fetch(image, size=1024)
        self.assertEqual(sorted(ImageThumbnail.objects.values_list("size", flat=True)), [256, 1024])

    def test_a_missing_cached_file_is_regenerated(self):
        image = self.photo(encode(noisy_rgb(800, 600)))
        self.fetch(image)
        thumbnail = ImageThumbnail.objects.get(image=image)
        thumbnail.file.storage.delete(thumbnail.file.name)
        response, body = self.fetch(image)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(body)).format, "JPEG")
        self.assertEqual(ImageThumbnail.objects.filter(image=image).count(), 1)

    def test_someone_elses_image_is_404(self):
        theirs = self.photo(encode(noisy_rgb(400, 300)), owner=self.other)
        self.assertEqual(self.client.get(self.url(theirs)).status_code, 404)
        self.assertEqual(self.client.get(reverse("image-thumbnail", kwargs={"pk": 999999})).status_code, 404)
        self.assertFalse(ImageThumbnail.objects.exists())

    def test_rejected_or_fileless_images_have_no_thumbnail(self):
        rejected = self.photo(encode(noisy_rgb(400, 300)), status="rejected")
        fileless = self.photo(b"", store=False)
        self.assertEqual(self.client.get(self.url(rejected)).status_code, 404)
        self.assertEqual(self.client.get(self.url(fileless)).status_code, 404)

    def test_an_undecodable_photo_is_a_clean_404_not_a_crash(self):
        image = self.photo(b"definitely not an image")
        response = self.client.get(self.url(image))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ImageThumbnail.objects.exists())

    def test_the_thumbnail_is_encrypted_at_rest_like_every_other_file(self):
        self.assertTrue(settings.IMAGE_ENCRYPTION_ENABLED)
        image = self.photo(encode(noisy_rgb(800, 600)))
        self.fetch(image)
        thumbnail = ImageThumbnail.objects.get(image=image)
        raw = Path(thumbnail.file.storage.path(thumbnail.file.name)).read_bytes()
        self.assertFalse(raw.startswith(b"\xff\xd8"), "the stored thumbnail is a plain JPEG")
        with thumbnail.file.storage.open(thumbnail.file.name, "rb") as stream:
            self.assertTrue(stream.read().startswith(b"\xff\xd8"))

    def test_the_file_lives_under_the_owners_thumbnail_folder(self):
        image = self.photo(encode(noisy_rgb(800, 600)))
        self.fetch(image)
        name = ImageThumbnail.objects.get(image=image).file.name
        self.assertTrue(name.startswith(f"users/{self.owner.pk}/thumbnails/"), name)

    def test_deleting_the_photo_removes_its_thumbnail_files(self):
        image = self.photo(encode(noisy_rgb(2000, 1000)))
        self.fetch(image, size=256)
        self.fetch(image, size=1024)
        thumbnails = list(ImageThumbnail.objects.filter(image=image))
        storage = thumbnails[0].file.storage
        names = [thumbnail.file.name for thumbnail in thumbnails]
        self.assertTrue(all(storage.exists(name) for name in names))
        image.delete()
        self.assertFalse(ImageThumbnail.objects.exists())
        self.assertFalse(any(storage.exists(name) for name in names))


class ConcurrencyTests(ThumbnailTestCase):
    def test_a_request_that_loses_the_race_keeps_the_winners_row_and_leaves_no_stray_file(self):
        image = self.photo(encode(noisy_rgb(800, 600)))
        real_render = render_thumbnail

        def winner_appears_first(field, size):
            result = real_render(field, size)
            ImageThumbnail.objects.create(
                image=image, owner=self.owner, size=size, width=1, height=1,
                file=f"users/{self.owner.pk}/thumbnails/winner.jpg",
            )
            return result

        folder = Path(settings.MEDIA_ROOT) / "users" / str(self.owner.pk) / "thumbnails"
        # What the loser leaves behind is the point, so compare against the
        # folder as it was. It is not empty to begin with on every database:
        # the media root is shared by the whole run, and SQLite reuses a primary
        # key after a rolled-back transaction, so an earlier test's user writes
        # into this same path. PostgreSQL never reuses one, which is why an
        # emptiness check happened to hold there and nowhere else.
        before = sorted(folder.glob("*")) if folder.exists() else []

        with patch("thumbnails.services.render_thumbnail", side_effect=winner_appears_first):
            result = get_or_create_thumbnail(image, 256)
        self.assertEqual(result.file.name, f"users/{self.owner.pk}/thumbnails/winner.jpg")
        self.assertEqual(ImageThumbnail.objects.filter(image=image).count(), 1)
        after = sorted(folder.glob("*")) if folder.exists() else []
        self.assertEqual(after, before, "the losing request left its rendered file behind")


class UrlExposureTests(ThumbnailTestCase):
    def test_the_image_list_links_to_the_thumbnail(self):
        image = self.photo(encode(noisy_rgb(400, 300)))
        response = self.client.get(reverse("image-upload-list"))
        row = next(item for item in response.data["results"] if item["id"] == image.pk) \
            if isinstance(response.data, dict) else next(item for item in response.data if item["id"] == image.pk)
        self.assertTrue(row["thumbnail_url"].endswith(f"/api/images/{image.pk}/thumbnail/"))

    def test_a_rejected_image_has_no_thumbnail_link(self):
        rejected = self.photo(encode(noisy_rgb(400, 300)), status="rejected")
        response = self.client.get(reverse("image-upload-detail", kwargs={"pk": rejected.pk}))
        self.assertIsNone(response.data["thumbnail_url"])

    @override_settings(THUMBNAIL_SIZES=(128, 512))
    def test_configured_sizes_are_used(self):
        image = self.photo(encode(noisy_rgb(1000, 500)))
        response, body = self.fetch(image)
        self.assertEqual(Image.open(io.BytesIO(body)).size, (128, 64))
        self.assertEqual(self.client.get(self.url(image, size=256)).status_code, 400)

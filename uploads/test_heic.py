"""iPhone photographs (HEIC / HEIF) are accepted and stored as a JPEG (uploads/heic.py)."""
import io
import tempfile
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image
from rest_framework.exceptions import ValidationError

from uploads import heic
from uploads.models import ImageUpload, UploadBatch
from uploads.services import _save_valid_image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except Exception:          # pillow-heif is optional at run time: these tests need it
    HAVE_HEIF = False

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-heic-")


def heic_bytes(size=(120, 80), orientation=None, taken="2025:03:31 04:12:04"):
    rng = np.random.default_rng(3)
    picture = Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8), "RGB")
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = taken                  # DateTimeOriginal, in the Exif IFD where a phone writes it
    if orientation:
        exif[0x0112] = orientation
    buffer = io.BytesIO()
    picture.save(buffer, "HEIF", quality=90, exif=exif.tobytes())
    return buffer.getvalue()


class AvailabilityTests(SimpleTestCase):
    def test_extensions_are_recognised_whatever_the_case(self):
        for name in ("IMG_0001.HEIC", "a.heic", "b.HEIF", "c.heif"):
            self.assertTrue(heic.is_heif_name(name), name)
        for name in ("a.jpg", "b.png", "heic", "c.heic.jpg"):
            self.assertFalse(heic.is_heif_name(name), name)

    def test_without_the_library_a_heic_is_refused_with_a_plain_sentence(self):
        with patch("uploads.heic.available", return_value=False), self.assertRaises(ValidationError) as caught:
            heic.to_jpeg(SimpleUploadedFile("a.heic", b"x"), "a.heic")
        self.assertIn("not supported on this server", str(caught.exception.detail))


@override_settings(MEDIA_ROOT=MEDIA, IMAGE_ENCRYPTION_ENABLED=False)
class HeicConversionTests(TestCase):
    def setUp(self):
        if not HAVE_HEIF:
            self.skipTest("pillow-heif is not installed.")
        self.user = User.objects.create_user(username="heic-owner", email="heic-owner@example.com", password="Password!123", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")

    def save(self, name, data):
        return _save_valid_image(self.batch, self.user, SimpleUploadedFile(name, data, content_type="image/heic"), name)

    def test_a_heic_is_stored_as_a_jpeg_with_its_own_name_and_a_jpg_ending(self):
        image = self.save("IMG_0001.HEIC", heic_bytes())
        self.assertEqual((image.original_name, image.file_extension, image.mime_type, image.status), ("IMG_0001.jpg", ".jpg", "image/jpeg", "uploaded"))
        with Image.open(image.original_file) as stored:
            self.assertEqual((stored.format, stored.size), ("JPEG", (120, 80)))

    def test_the_capture_time_survives_the_conversion(self):
        image = self.save("IMG_0002.heic", heic_bytes(taken="2025:03:31 04:12:04"))
        self.assertEqual(image.taken_at.strftime("%Y-%m-%d %H:%M:%S"), "2025-03-31 04:12:04")

    def test_the_orientation_is_applied_to_the_pixels_and_not_left_to_rotate_them_again(self):
        image = self.save("IMG_0003.heic", heic_bytes(size=(120, 80), orientation=6))          # 6: rotate 90 degrees clockwise to display
        with Image.open(image.original_file) as stored:
            self.assertEqual(stored.size, (80, 120))
            self.assertNotIn(0x0112, dict(stored.getexif()))

    def test_a_file_that_is_not_a_heif_is_refused_even_with_the_extension(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "JPEG")
        with self.assertRaises(ValidationError):
            self.save("fake.heic", buffer.getvalue())
        with self.assertRaises(ValidationError):
            self.save("broken.heic", b"not an image at all")

    @override_settings(MAX_IMAGE_PIXELS=1000)
    def test_the_pixel_limit_is_checked_before_the_picture_is_decoded(self):
        with self.assertRaises(ValidationError) as caught:
            self.save("big.heic", heic_bytes(size=(120, 80)))
        self.assertIn("pixel limit", str(caught.exception.detail))

    def test_the_stored_photograph_is_listed_like_any_other_upload(self):
        self.save("IMG_0004.heic", heic_bytes())
        self.assertEqual(ImageUpload.objects.filter(owner=self.user, status="uploaded", original_name="IMG_0004.jpg").count(), 1)

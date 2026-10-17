"""The real readers of an original: preprocessing, enhancement, face metrics, thumbnails.

Real decoding through each reader, on files stored like uploads. References:
the PNG sample-scaling rule and the published Adobe RGB/sRGB colorants.
"""
import io

import cv2
import numpy as np
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from PIL import Image

from accounts.models import User
from enhancement.models import EnhancementLevel, ImageEnhancementResult
from enhancement.services import _read_full_resolution_original as enhancement_reader
from thumbnails.services import render_thumbnail
from validation.services import _read_full_resolution_original as face_metrics_reader

from .models import ImageUpload, UploadBatch
from .services import preprocess_image
from .test_decoding_refs import ADOBE_GAMMA, ADOBE_RGB_D50, adobe_to_srgb_reference, gradient16, matrix_trc_profile


def png_bytes(image, **options):
    buffer = io.BytesIO()
    image.save(buffer, "PNG", **options)
    return buffer.getvalue()


class UploadFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="decoding", email="decoding@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")

    def upload(self, payload, name="photo.png"):
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name=name, file_extension=".png", mime_type="image/png",
            file_size=len(payload), status="uploaded",
        )
        image.original_file.save(name, ContentFile(payload))
        return image


@override_settings(DUPLICATE_AUTO_ANALYZE=False, IDENTITY_AUTO_ANALYZE=False)
class ReaderTests(UploadFixture):
    def every_reader(self, image):
        """The decoded original as each reader sees it (RGB, uint8)."""
        result, _ = preprocess_image(image, requested_format="png")
        with result.processed_file.open("rb") as handle:
            preprocessed = np.asarray(Image.open(io.BytesIO(handle.read())).convert("RGB"))
        enhancement = ImageEnhancementResult(owner=self.user, image=image, preprocessing_result=result,
                                             requested_level=EnhancementLevel.LOW)
        thumbnail, _width, _height = render_thumbnail(image.original_file, 4096)
        return {
            "preprocessing": preprocessed,
            "enhancement": cv2.cvtColor(enhancement_reader(enhancement), cv2.COLOR_BGR2RGB),
            "face_metrics": cv2.cvtColor(face_metrics_reader(image), cv2.COLOR_BGR2RGB),
            "thumbnail": np.asarray(Image.open(io.BytesIO(thumbnail)).convert("RGB")),
        }

    def test_a_16_bit_grayscale_png_keeps_its_tonal_range_in_every_reader(self):
        values = gradient16(height=16, width=512)
        expected = np.rint(values.astype(np.float64) * 255 / 65535)
        readers = self.every_reader(self.upload(png_bytes(Image.fromarray(values))))
        for name, decoded in readers.items():
            with self.subTest(reader=name):
                grey = decoded[..., 0].astype(np.float64)
                self.assertGreaterEqual(len(np.unique(grey)), 250)
                tolerance = 3 if name == "thumbnail" else 0  # the thumbnail is a JPEG
                self.assertLessEqual(float(np.abs(grey - expected).mean()), tolerance + 0.01)

    def test_an_adobe_rgb_png_is_converted_to_srgb_in_every_reader(self):
        samples = np.repeat(np.repeat(np.array([[[200, 120, 80], [40, 180, 90]]], dtype=np.uint8), 8, 0), 8, 1)
        profile = matrix_trc_profile(ADOBE_RGB_D50, ADOBE_GAMMA, "Test Adobe RGB (1998)")
        readers = self.every_reader(self.upload(png_bytes(Image.fromarray(samples), icc_profile=profile)))
        reference = adobe_to_srgb_reference(samples[4:5, [4, 12]])
        for name, decoded in readers.items():
            with self.subTest(reader=name):
                tolerance = 3 if name == "thumbnail" else 1
                difference = np.abs(decoded[4:5, [4, 12]].astype(int) - reference)
                self.assertLessEqual(int(difference.max()), tolerance, decoded[4:5, [4, 12]])

    def test_the_original_file_is_left_unchanged(self):
        payload = png_bytes(Image.fromarray(gradient16(height=8, width=64)))
        image = self.upload(payload)
        self.every_reader(image)
        with image.original_file.open("rb") as handle:
            self.assertEqual(handle.read(), payload)


@override_settings(
    DUPLICATE_AUTO_ANALYZE=False, IDENTITY_AUTO_ANALYZE=False,
    HD_RESTORATION_ENABLED=False, NAFNET_DEBLUR_ENABLED=False, GFPGAN_ENABLED=False, ZERO_DCE_ENABLED=False,
    IDENTITY_GUARD_ENABLED=False,
)
class EncodedOutputTests(UploadFixture):
    """The stored enhancement output, decoded again (classical stages only)."""

    def enhance(self, payload):
        from enhancement.services import create_enhancement_result, process_enhancement_result

        image = self.upload(payload)
        preprocess_image(image, requested_format="png")
        image.refresh_from_db()
        result = process_enhancement_result(create_enhancement_result(image, EnhancementLevel.LOW))
        with result.enhanced_file.open("rb") as handle:
            stored = np.asarray(Image.open(io.BytesIO(handle.read())).convert("RGB"))
        return result, stored

    def test_a_16_bit_grayscale_original_gives_a_full_range_output(self):
        result, stored = self.enhance(png_bytes(Image.fromarray(gradient16(height=32, width=512))))
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(len(np.unique(stored[..., 1])), 200)
        self.assertEqual(result.applied_parameters["source_decoding"]["bits_per_sample"], 16)

    def test_an_adobe_rgb_original_is_recorded_as_converted(self):
        samples = np.repeat(np.repeat(np.array([[[200, 120, 80], [40, 180, 90]]], dtype=np.uint8), 16, 0), 16, 1)
        profile = matrix_trc_profile(ADOBE_RGB_D50, ADOBE_GAMMA, "Test Adobe RGB (1998)")
        result, stored = self.enhance(png_bytes(Image.fromarray(samples), icc_profile=profile))
        decoding = result.applied_parameters["source_decoding"]["colour_profile"]
        self.assertEqual((decoding["description"], decoding["converted_to"]), ("Test Adobe RGB (1998)", "sRGB"))
        reference = adobe_to_srgb_reference(samples[8:9, [8, 24]])
        # The enhancement may adjust colour a little; it must start from sRGB, not the raw Adobe numbers.
        self.assertLess(np.abs(stored[8:9, [8, 24]].astype(int) - reference).mean(),
                        np.abs(stored[8:9, [8, 24]].astype(int) - samples[8:9, [8, 24]].astype(int)).mean())

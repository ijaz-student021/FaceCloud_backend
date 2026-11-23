"""A multi-picture JPEG (MPO: what a phone writes for a portrait or a depth photograph) is a JPEG, not "Unsupported image content".

Found 2026-11-23: an iPhone photograph (IMG_1867.jpg, 4032 x 3024, two pictures, ``PIL.Image.format == "MPO"``) was rejected at upload and never stored.
"""
import io

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.exceptions import ValidationError

from thumbnails.services import render_thumbnail
from uploads.validators import validate_image_file


def mpo_bytes(size=(64, 48)):
    rng = np.random.default_rng(5)
    first = Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8), "RGB")
    second = Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8), "RGB")
    buffer = io.BytesIO()
    first.save(buffer, "MPO", save_all=True, append_images=[second])
    return buffer.getvalue()


class MpoTests(SimpleTestCase):
    def test_the_fixture_really_is_an_mpo_to_pillow(self):
        with Image.open(io.BytesIO(mpo_bytes())) as image:
            self.assertEqual((image.format, getattr(image, "n_frames", 1)), ("MPO", 2))

    def test_a_multi_picture_jpeg_is_accepted_as_a_jpeg(self):
        upload = SimpleUploadedFile("IMG_1867.jpg", mpo_bytes(), content_type="image/jpeg")
        metadata = validate_image_file(upload)
        self.assertEqual((metadata["file_extension"], metadata["mime_type"], metadata["width"], metadata["height"]), (".jpg", "image/jpeg", 64, 48))

    def test_it_still_has_to_carry_a_jpeg_extension(self):
        with self.assertRaises(ValidationError):
            validate_image_file(SimpleUploadedFile("IMG_1867.png", mpo_bytes(), content_type="image/png"))

    def test_a_format_that_is_not_supported_is_still_refused(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "PPM")
        with self.assertRaises(ValidationError):
            validate_image_file(SimpleUploadedFile("a.jpg", buffer.getvalue(), content_type="image/jpeg"))

    def test_a_multi_picture_jpeg_gets_a_thumbnail(self):
        class Field:
            def __init__(self, data):
                self.data = data

            def open(self, mode):
                self.stream = io.BytesIO(self.data)

            def read(self, *args):
                return self.stream.read(*args)

            def seek(self, *args):
                return self.stream.seek(*args)

            def tell(self):
                return self.stream.tell()

            def close(self):
                pass

        field = Field(mpo_bytes((400, 300)))
        field.open("rb")
        payload, width, height = render_thumbnail(field, 256)
        self.assertEqual((width, height), (256, 192))
        self.assertTrue(payload.startswith(b"\xff\xd8"))

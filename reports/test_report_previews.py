"""The PDF report shows a small picture of each photo (the first PREVIEW_LIMIT), the enhanced one when there is one; a photo without a file just has none.

A report never fails because of a picture: a missing, unreadable or unknown photo is left out, and a report with no pictures keeps its old layout.
"""
import io
import tempfile
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from PIL import Image

from enhancement.models import ImageEnhancementResult
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from . import renderers
from .renderers import render_pdf
from .test_readable_reports import batch_payload, record, render_text

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="facecloud-report-")


def jpeg(seed, size=(400, 300)):
    pixels = np.random.default_rng(seed).integers(0, 255, size=(size[1], size[0], 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, "JPEG")
    return buffer.getvalue()


def pictures_in(pdf):
    return pdf.count(b"/Subtype /Image")


@override_settings(MEDIA_ROOT=MEDIA, IMAGE_ENCRYPTION_ENABLED=False)
class ReportPreviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="report-owner", email="report-owner@example.com", password="Password!123", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.count = 0

    def photo(self, seed, *, status="uploaded", store=True):
        self.count += 1
        image = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name=f"p{self.count}.jpg", file_extension=".jpg", mime_type="image/jpeg", status=status)
        if store:
            image.original_file.save(image.original_name, ContentFile(jpeg(seed)))
        return image

    def pdf(self, *records):
        return render_pdf(batch_payload(list(records)))

    def test_a_photo_with_a_file_gets_a_picture_and_a_preview_column(self):
        image = self.photo(1)
        payload = batch_payload([record(image.pk, image.original_name)])
        self.assertEqual(pictures_in(render_pdf(payload)), 1)
        self.assertIn("Preview", render_text(payload))

    def test_a_rejected_photo_is_previewed_in_both_tables(self):
        image = self.photo(2, status="rejected")
        pdf = self.pdf(record(image.pk, image.original_name, status="rejected", reasons=["severe_blur"]))
        self.assertGreaterEqual(pictures_in(pdf), 1)

    def test_the_enhanced_picture_is_used_when_there_is_one(self):
        from thumbnails import services

        image = self.photo(3)
        preprocessing = ImagePreprocessingResult.objects.create(
            image=image, owner=self.user, status="completed", output_format="png", output_name="p.png", output_width=400, output_height=300, processed_file_size=1)
        result = ImageEnhancementResult.objects.create(image=image, owner=self.user, preprocessing_result=preprocessing, status="completed", requested_level="auto")
        result.enhanced_file.save("enhanced-copy.jpg", ContentFile(jpeg(99)))
        seen, original = [], services.render_thumbnail
        with patch.object(services, "render_thumbnail", side_effect=lambda field, size: (seen.append(field.name), original(field, size))[1]):
            self.pdf(record(image.pk, image.original_name))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen, [result.enhanced_file.name])                                       # the enhanced file, not the original

    def test_a_photo_without_a_file_or_an_unknown_id_has_no_picture_and_the_old_layout(self):
        gone = self.photo(4, store=False)
        pdf = self.pdf(record(gone.pk, "gone.jpg"), record(999999, "unknown.jpg"))
        self.assertEqual(pictures_in(pdf), 0)
        self.assertNotIn("Preview", render_text(batch_payload([record(gone.pk, "gone.jpg")])))

    def test_an_unreadable_file_does_not_fail_the_report(self):
        broken = self.photo(5, store=False)
        broken.original_file.save("broken.jpg", ContentFile(b"not an image at all"))
        good = self.photo(6)
        pdf = self.pdf(record(broken.pk, "broken.jpg"), record(good.pk, good.original_name))
        self.assertEqual(pictures_in(pdf), 1)                                                   # the readable one still has its picture

    def test_only_the_first_photos_get_a_picture(self):
        images = [self.photo(10 + index) for index in range(4)]
        with patch.object(renderers, "PREVIEW_LIMIT", 2):
            pdf = self.pdf(*[record(image.pk, image.original_name) for image in images])
        self.assertEqual(pictures_in(pdf), 2)

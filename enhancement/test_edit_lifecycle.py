"""Saved edits: transparency, ownership of the render, deletion during a render.

Classical processing; the queue is not involved (tasks are called directly).
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.core.files.base import ContentFile
from django.test import override_settings

from uploads.models import ImagePreprocessingResult, ImageUpload

from .models import EditMode, EnhancementLevel, EnhancementStatus, ImageEditVersion, ImageEnhancementResult
from .services import process_enhancement_result, process_image_edit
from .test_pipeline_regressions import NO_MODELS, ResultFixture, textured


def alpha_png(height=64, width=96):
    """A photo-like PNG whose left third is fully transparent and whose right edge is half transparent."""
    bgr = textured(height, width)
    alpha = np.full((height, width), 255, np.uint8)
    alpha[:, : width // 3] = 0
    alpha[:, -8:] = 128
    return cv2.imencode(".png", np.dstack([bgr, alpha]))[1].tobytes(), alpha


@override_settings(**NO_MODELS)
class EditLifecycleFixture(ResultFixture):
    def make_png_result(self):
        payload, alpha = alpha_png()
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="cutout.png", file_extension=".png",
            mime_type="image/png", file_size=len(payload), width=96, height=64, status="uploaded",
        )
        image.original_file.save("cutout.png", ContentFile(payload))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="png", output_name="cutout.png",
            output_width=96, output_height=64, processed_file_size=len(payload), had_transparency=True,
        )
        preprocessing.processed_file.save("cutout.png", ContentFile(payload))
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=image, preprocessing_result=preprocessing, status="queued",
            requested_level=EnhancementLevel.LOW,
        )
        return process_enhancement_result(result), alpha

    def make_edit(self, result, **fields):
        return ImageEditVersion.objects.create(
            owner=self.user, enhancement_result=result, mode=EditMode.MANUAL,
            requested_adjustments={"brightness": 12, "contrast": 8}, status=EnhancementStatus.QUEUED, **fields,
        )

    def stored_alpha(self, edit):
        with edit.edited_file.open("rb") as handle:
            decoded = cv2.imdecode(np.frombuffer(handle.read(), np.uint8), cv2.IMREAD_UNCHANGED)
        return decoded


class DefectDemonstrationTests(EditLifecycleFixture):
    def test_editing_a_png_with_transparency_keeps_its_alpha(self):
        result, alpha = self.make_png_result()
        edit = process_image_edit(self.make_edit(result))
        decoded = self.stored_alpha(edit)
        self.assertEqual(decoded.shape[2], 4, "the edited PNG lost its alpha channel")
        np.testing.assert_array_equal(decoded[:, :, 3], alpha)

    def test_deleting_an_edit_while_it_renders_does_not_bring_it_back(self):
        result = process_enhancement_result(self.make_result("plain.jpg"))
        edit = self.make_edit(result)
        real = __import__("enhancement.services", fromlist=["apply_gallery_adjustments"]).apply_gallery_adjustments

        def delete_then_render(*args, **kwargs):
            ImageEditVersion.objects.filter(pk=edit.pk).delete()  # the user cancelled mid-render
            return real(*args, **kwargs)

        with patch("enhancement.services.apply_gallery_adjustments", side_effect=delete_then_render):
            try:
                process_image_edit(edit)
            except Exception:
                pass
        self.assertFalse(ImageEditVersion.objects.filter(pk=edit.pk).exists(), "a deleted edit was resurrected")

    def test_a_second_delivery_does_not_render_while_the_first_is_running(self):
        from django.utils import timezone

        from .services import EditInProgress

        result = process_enhancement_result(self.make_result("plain.jpg"))
        edit = self.make_edit(result)
        ImageEditVersion.objects.filter(pk=edit.pk).update(  # the first worker holds a live claim
            status=EnhancementStatus.PROCESSING, claim_token="a" * 32, claimed_at=timezone.now(),
        )
        with patch("enhancement.services.apply_gallery_adjustments") as render:
            with self.assertRaises(EditInProgress):
                process_image_edit(edit)
        render.assert_not_called()
        self.assertEqual(ImageEditVersion.objects.get(pk=edit.pk).claim_token, "a" * 32)  # not disturbed

    def test_a_claim_that_was_never_renewed_is_taken_over(self):
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

        result = process_enhancement_result(self.make_result("plain.jpg"))
        edit = self.make_edit(result)
        ImageEditVersion.objects.filter(pk=edit.pk).update(
            status=EnhancementStatus.PROCESSING, claim_token="a" * 32,
            claimed_at=timezone.now() - timedelta(seconds=settings.ENHANCEMENT_CLAIM_LEASE_SECONDS + 5),
        )
        self.assertEqual(process_image_edit(edit).status, "completed")

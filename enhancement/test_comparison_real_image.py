"""Integration check on a real photograph, skipped when the sample is not present."""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, tag
from unittest import skipUnless

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .comparison import process_model_comparison
from .models import ModelComparison

User = get_user_model()
SAMPLE = Path(settings.BASE_DIR) / ".runtime" / "dataset_review" / "image" / "30.png"
ZERO_DCE_MODEL = Path(settings.BASE_DIR) / settings.ZERO_DCE_MODEL_PATH


@tag("slow")
@skipUnless(SAMPLE.exists() and settings.ZERO_DCE_ENABLED and ZERO_DCE_MODEL.exists(), "sample photograph or Zero-DCE model not available")
class RealPhotoComparisonTests(TestCase):
    def test_a_dark_street_photo_is_improved_by_at_least_one_profile(self):
        user = User.objects.create_user(
            username="real-photo", email="real-photo@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=user, upload_type="single")
        payload = SAMPLE.read_bytes()
        image = ImageUpload.objects.create(
            owner=user, batch=batch, original_name="30.png", status="uploaded",
            file_extension=".png", mime_type="image/png", file_size=len(payload),
        )
        image.original_file.save("30.png", ContentFile(payload))
        pre = ImagePreprocessingResult.objects.create(
            owner=user, image=image, status="completed", output_format="png",
        )
        pre.processed_file.save("30.png", ContentFile(payload))
        comparison = ModelComparison.objects.create(
            owner=user, image=image, status="queued",
            profiles=["opencv_baseline", "natural", "dynamic", "ai_max"],
        )

        comparison = process_model_comparison(comparison)

        self.assertEqual(comparison.status, "completed")
        outputs = {output.profile: output for output in comparison.outputs.all()}
        self.assertEqual(set(outputs), {"original", "opencv_baseline", "natural", "dynamic", "ai_max"})
        source_luminance = outputs["original"].metrics["mean_luminance"]
        self.assertLess(source_luminance, 30)  # a genuinely dark photograph
        for name, output in outputs.items():
            self.assertEqual(output.outcome, "completed", name)
            self.assertTrue(output.preview_file, name)
            if name != "original":
                self.assertGreater(output.metrics["mean_luminance"], source_luminance, name)
        # The baseline never uses the learned model; the default pipeline does.
        self.assertNotIn("zero_dce", outputs["opencv_baseline"].algorithm)
        self.assertIn("zero_dce", outputs["natural"].algorithm)
        # The learned model is what rescues a photo this dark.
        self.assertGreater(
            outputs["natural"].metrics["mean_luminance"],
            outputs["opencv_baseline"].metrics["mean_luminance"] * 2,
        )
        self.assertIn(comparison.recommended_profile, {"natural", "dynamic", "ai_max"})
        self.assertIn("against", comparison.recommendation_reason)

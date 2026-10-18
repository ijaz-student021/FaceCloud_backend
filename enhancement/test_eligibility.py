"""A rejected photograph is never enhanced, by the batch request or the single request."""
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APIClient

from quality_control.models import ImageQualityDecision
from validation.models import FaceValidationResult

from .eligibility import REFUSAL, not_rejected, rejection_of
from .models import EnhancementBatchJob, ImageEnhancementResult
from .test_pipeline_regressions import NO_MODELS, ResultFixture
from uploads.models import ImageUpload


@override_settings(**NO_MODELS)
class EligibilityTests(ResultFixture):
    def photo(self, name, *, quality=None, face=None, upload_status="uploaded"):
        """A preprocessed photograph with no enhancement yet, and the given check outcomes."""
        result = self.make_result(name)
        image = result.image
        preprocessing = result.preprocessing_result
        result.delete()
        if upload_status != "uploaded":
            ImageUpload.objects.filter(pk=image.pk).update(status=upload_status)
        if quality:
            ImageQualityDecision.objects.create(
                owner=self.user, image=image, preprocessing_result=preprocessing, status="completed", decision=quality,
            )
        if face:
            FaceValidationResult.objects.create(
                owner=self.user, image=image, preprocessing_result=preprocessing, decision=face, face_count=1,
            )
        return ImageUpload.objects.get(pk=image.pk)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_table_in_the_module_docstring_holds(self):
        cases = {
            "undecided": (dict(), True),
            "accepted": (dict(quality="accepted", face="accepted"), True),
            "needs review": (dict(quality="needs_review", face="needs_review"), True),
            "quality rejected": (dict(quality="rejected"), False),
            "face rejected": (dict(face="rejected"), False),
            "refused at upload": (dict(upload_status="rejected"), False),
        }
        for label, (checks, allowed) in cases.items():
            with self.subTest(label):
                image = self.photo(f"{label}.jpg", **checks)
                self.assertEqual(rejection_of(image) is None, allowed)
                self.assertEqual(not_rejected(ImageUpload.objects.filter(pk=image.pk)).exists(), allowed)
                image.delete()

    def test_a_batch_enhances_the_accepted_photos_and_skips_the_rejected_ones(self):
        good = self.photo("good.jpg", quality="accepted", face="accepted")
        review = self.photo("review.jpg", quality="needs_review")
        bad = self.photo("bad.jpg", quality="rejected")
        no_face = self.photo("noface.jpg", quality="accepted", face="rejected")
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"level": "auto"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        enhanced = set(ImageEnhancementResult.objects.values_list("image_id", flat=True))
        self.assertEqual(enhanced, {good.pk, review.pk})
        self.assertEqual(EnhancementBatchJob.objects.get().total_images, 2)
        self.assertFalse({bad.pk, no_face.pk} & enhanced)

    def test_a_batch_of_only_rejected_photos_starts_nothing_and_says_so(self):
        self.photo("bad.jpg", quality="rejected")
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"level": "auto"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EnhancementBatchJob.objects.exists())
        self.assertFalse(ImageEnhancementResult.objects.exists())

    def test_one_rejected_photo_is_refused_with_the_sentence_and_creates_nothing(self):
        bad = self.photo("bad.jpg", quality="rejected")
        with patch("enhancement.tasks.process_enhancement_image.apply_async") as queued:
            response = self.client.post(f"/api/enhancements/images/{bad.pk}/", {"level": "auto"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn(REFUSAL, str(response.json()))
        queued.assert_not_called()
        self.assertFalse(ImageEnhancementResult.objects.exists())

    def test_one_accepted_photo_is_still_enhanced(self):
        good = self.photo("good.jpg", quality="accepted")
        with patch("enhancement.tasks.process_enhancement_image.apply_async"):
            response = self.client.post(f"/api/enhancements/images/{good.pk}/", {"level": "auto"}, format="json")
        self.assertEqual(response.status_code, 202, response.content)

"""A photograph rejected only for being very dark: ``reject`` by default, ``needs_review`` when QUALITY_UNDEREXPOSED_FACE_ACTION=review.

Why it matters: a rejected photograph is never enhanced (enhancement/eligibility.py), and low light is what enhancement is for. Blur and a low resolution
already never reject a photograph with a face (the restoration chain handles them); this is the same rule for darkness, offered as an opt-in. The setting
only ever moves a photograph from *rejected* to *needs_review*, and only when darkness is the only reason: it accepts nothing, and enhancement still has
to pass the engine's own gates (the original is returned unless a candidate does).
"""
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from enhancement.models import ImageEnhancementResult
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import ImageQualityDecision
from .services import calculate_scores, make_decision, process_quality_decision

DARK = dict(mean_luminance=16.0, contrast_stddev=8.0)             # below both reject limits (25 and 12)
LOW_CONTRAST = dict(mean_luminance=110.0, contrast_stddev=8.0)    # normally lit, almost no contrast (below 12)
DIM = dict(mean_luminance=40.0, contrast_stddev=30.0)            # in the review band only (25-55): never rejected


def metrics(**override):
    base = dict(
        width=480, height=640, mean_luminance=120.0, contrast_stddev=45.0, laplacian_sharpness=90.0, estimated_noise=3.0,
        shadow_clipping_percent=0.2, highlight_clipping_percent=0.2, sharpness_basis="face",
    )
    return {**base, **override}


def face(decision="accepted", count=1, reason=""):
    return SimpleNamespace(face_count=count, decision=decision, rejection_reason=reason)


def decide(face_validation, **override):
    values = metrics(**override)
    return make_decision(values, calculate_scores(values), face_validation)


class MakeDecisionDefaultTests(SimpleTestCase):
    """The default is unchanged: very dark is rejected, with a face or without."""

    def test_severe_underexposure_with_a_face_is_rejected(self):
        decision, reasons, _message = decide(face(), **DARK)
        self.assertEqual(decision, "rejected")
        self.assertIn("severe_underexposure", reasons)

    def test_low_contrast_with_a_face_is_rejected(self):
        decision, reasons, _message = decide(face(), **LOW_CONTRAST)
        self.assertEqual(decision, "rejected")
        self.assertIn("contrast_too_low", reasons)

    def test_the_default_setting_is_reject(self):
        from django.conf import settings

        self.assertEqual(settings.QUALITY_UNDEREXPOSED_FACE_ACTION, "reject")


@override_settings(QUALITY_UNDEREXPOSED_FACE_ACTION="review")
class MakeDecisionReviewTests(SimpleTestCase):
    def test_severe_underexposure_with_an_accepted_face_is_held_for_review_not_accepted(self):
        decision, reasons, message = decide(face(), **DARK)
        self.assertEqual(decision, "needs_review")
        self.assertIn("severe_underexposure", reasons)             # the reason is kept: the review (and the member) still sees why
        self.assertIn("contrast_too_low", reasons)
        self.assertIn("review", message)

    def test_low_contrast_alone_with_a_face_is_held_for_review(self):
        decision, reasons, _message = decide(face(), **LOW_CONTRAST)
        self.assertEqual((decision, reasons), ("needs_review", ["contrast_too_low"]))

    def test_a_face_the_face_check_wants_reviewed_is_still_reviewed(self):
        decision, reasons, _message = decide(face("needs_review"), **DARK)
        self.assertEqual(decision, "needs_review")
        self.assertIn("face_validation_required", reasons)

    def test_without_a_face_it_stays_rejected(self):
        for validation in (face(count=0, decision="rejected", reason="no_face_detected"), face(count=0), None):
            with self.subTest(validation=validation):
                self.assertEqual(decide(validation, **DARK)[0], "rejected")

    def test_a_face_the_face_check_rejected_stays_rejected(self):
        decision, reasons, _message = decide(face("rejected", reason="face_too_small"), **DARK)
        self.assertEqual(decision, "rejected")
        self.assertIn("face_too_small", reasons)

    def test_any_other_reject_reason_keeps_it_rejected(self):
        """Darkness is the only thing the setting softens: a tiny photograph, or a blown-out one with no contrast, is not rescued."""
        self.assertEqual(decide(face(), width=100, height=120, **DARK)[0], "rejected")                          # resolution_too_low as well
        decision, reasons, _message = decide(face(), mean_luminance=240.0, contrast_stddev=8.0)                  # severe_overexposure + low contrast
        self.assertEqual(decision, "rejected")
        self.assertIn("severe_overexposure", reasons)

    def test_a_photograph_that_was_never_rejected_is_unchanged(self):
        self.assertEqual(decide(face())[0], "accepted")
        self.assertEqual(decide(face(), **DIM)[0], "needs_review")                 # the review band already worked this way

    def test_a_clean_photograph_is_not_made_to_wait(self):
        self.assertEqual(decide(face(), mean_luminance=130.0, contrast_stddev=55.0, laplacian_sharpness=140.0)[0], "accepted")


def dark_png(size=(480, 360), level=16):
    """A small dark frame with faint texture: mean luminance about ``level``."""
    rng = np.random.default_rng(1)
    frame = np.clip(level + rng.normal(0, 2.5, (size[0], size[1], 3)), 0, 255).astype(np.uint8)
    return cv2.imencode(".png", frame)[1].tobytes()


@override_settings(MEDIA_ROOT=__import__("tempfile").mkdtemp(), CELERY_TASK_ALWAYS_EAGER=True)
class DarkPhotoEndToEndTests(TestCase):
    """The stored decision and the enhancement request, through the real quality task code and the real endpoint."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="dark-owner", email="dark-owner@example.com", password="ForestRiver!4821",
                                                         is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def dark_photo(self):
        encoded = dark_png()
        image = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name="dark.png", file_extension=".png", mime_type="image/png",
                                           file_size=len(encoded), width=360, height=480, status="uploaded")
        image.original_file.save("dark.png", ContentFile(encoded))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="png", output_name="dark.png", output_width=360, output_height=480,
            processed_file_size=len(encoded))
        preprocessing.processed_file.save("dark.png", ContentFile(encoded))
        validation = FaceValidationResult.objects.create(
            owner=self.user, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1,
            faces=[{"x": 100, "y": 120, "width": 160, "height": 200, "confidence": 0.9, "area_ratio": 0.18}])
        decision = ImageQualityDecision.objects.create(owner=self.user, image=image, preprocessing_result=preprocessing, face_validation=validation,
                                                       status="queued")
        process_quality_decision(decision.pk)
        return image, ImageQualityDecision.objects.get(pk=decision.pk)

    def request_enhancement(self, image):
        with patch("enhancement.views.process_enhancement_image.apply_async"):
            return self.client.post(f"/api/enhancements/images/{image.pk}/", {"level": "auto", "style": "natural"}, format="json")

    def test_by_default_a_very_dark_photograph_is_rejected_and_the_request_is_refused_with_nothing_created(self):
        image, decision = self.dark_photo()
        self.assertEqual(decision.decision, "rejected")
        self.assertIn("severe_underexposure", decision.rejection_reasons)
        response = self.request_enhancement(image)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), ["This photo was rejected by the checks, so it is not enhanced."])
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())

    @override_settings(QUALITY_UNDEREXPOSED_FACE_ACTION="review")
    def test_with_the_opt_in_it_waits_for_review_and_may_be_enhanced_but_nothing_is_accepted_for_it(self):
        image, decision = self.dark_photo()
        self.assertEqual(decision.decision, "needs_review")
        self.assertEqual(decision.status, "completed")
        self.assertIn("severe_underexposure", decision.rejection_reasons)
        response = self.request_enhancement(image)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertTrue(ImageEnhancementResult.objects.filter(image=image).exists())

    @override_settings(QUALITY_UNDEREXPOSED_FACE_ACTION="review")
    def test_a_dark_photograph_with_no_face_is_still_refused(self):
        image, _decision = self.dark_photo()
        FaceValidationResult.objects.filter(image=image).update(decision="rejected", face_count=0, faces=[], rejection_reason="no_face_detected")
        ImageQualityDecision.objects.filter(image=image).update(status="queued", decision="")
        decision = process_quality_decision(ImageQualityDecision.objects.get(image=image).pk)
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(self.request_enhancement(image).status_code, 400)

    @override_settings(QUALITY_UNDEREXPOSED_FACE_ACTION="review")
    def test_the_original_file_is_never_touched(self):
        image, _decision = self.dark_photo()
        before = image.original_file.read()
        image.original_file.close()
        self.request_enhancement(image)
        image.refresh_from_db()
        after = image.original_file.read()
        image.original_file.close()
        self.assertEqual(before, after)

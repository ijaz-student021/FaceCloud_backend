"""Enlarging a very small photograph during preprocessing (SMALL_IMAGE_ENLARGE_ENABLED, on by default).

The decisions (when, by how much, with what, what happens when the model cannot be used) are tested with stand-ins for the model, because they are
decisions. What the real model does to real photographs is in docs/evaluation/2026-11-02-small-image-enlargement.md.
"""
import hashlib
import io
import os
import tempfile
from unittest import skipUnless
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from accounts.models import User

from .enlarge import OPENCV_ENGINE, REALESRGAN_ENGINE, enlarge_small_image, enlargement_scale
from .models import ImageUpload, UploadBatch
from .serializers import ImagePreprocessingResultSerializer
from .services import preprocess_image

ON = dict(SMALL_IMAGE_ENLARGE_ENABLED=True)


def small_picture(width=150, height=200, mode="RGB"):
    """A textured picture (smooth colour blobs), so that a resize is measurable."""
    rng = np.random.default_rng(3)
    blobs = cv2.resize(rng.integers(0, 255, (9, 12, 3), dtype=np.uint8), (width, height), interpolation=cv2.INTER_CUBIC)
    image = Image.fromarray(cv2.cvtColor(blobs, cv2.COLOR_BGR2RGB))
    if mode == "RGBA":
        alpha = np.full((height, width), 255, np.uint8)
        alpha[:, : width // 2] = 0
        image = image.convert("RGBA")
        image.putalpha(Image.fromarray(alpha))
    return image


def png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def fake_model(bgr, output_scale=None):
    """A stand-in for Real-ESRGAN: a cubic enlargement (so it always agrees with the input)."""
    scale = int(output_scale)
    return cv2.resize(bgr, (bgr.shape[1] * scale, bgr.shape[0] * scale), interpolation=cv2.INTER_CUBIC), {"provider": "stand-in"}


class PlanningTests(SimpleTestCase):
    def test_it_is_on_by_default_and_can_be_turned_off(self):
        self.assertIs(settings.SMALL_IMAGE_ENLARGE_ENABLED, True)
        self.assertEqual(enlargement_scale(100, 100), 4)
        with override_settings(SMALL_IMAGE_ENLARGE_ENABLED=False):
            self.assertEqual(enlargement_scale(100, 100), 0)

    def test_a_picture_that_four_times_would_not_rescue_is_left_alone(self):
        self.assertEqual(enlargement_scale(60, 80), 4)          # 240: reaches the quality check's line
        self.assertEqual(enlargement_scale(59, 80), 0)          # 236: still under it, so enlarging only invents pixels
        self.assertEqual(enlargement_scale(16, 512), 0)
        self.assertEqual(enlargement_scale(10, 5), 0)
        self.assertEqual(enlargement_scale(0, 100), 0)

    @override_settings(**ON)
    def test_the_defaults_follow_the_quality_check(self):
        self.assertEqual(settings.SMALL_IMAGE_ENLARGE_BELOW_EDGE, settings.QUALITY_REJECT_MIN_EDGE)
        self.assertEqual(settings.SMALL_IMAGE_ENLARGE_TARGET_EDGE, settings.QUALITY_REVIEW_MIN_EDGE)
        self.assertEqual(settings.SMALL_IMAGE_ENLARGE_ENGINE, "auto")

    @override_settings(**ON)
    def test_only_a_photograph_under_the_line_is_enlarged(self):
        self.assertEqual(enlargement_scale(150, 200), 4)        # 300 would still be under the target
        self.assertEqual(enlargement_scale(239, 400), 4)        # 478 is under 480
        self.assertEqual(enlargement_scale(240, 400), 0)        # not under the quality check's line
        self.assertEqual(enlargement_scale(1000, 150), 4)       # the shorter side decides, whichever it is
        self.assertEqual(enlargement_scale(2000, 2000), 0)

    @override_settings(SMALL_IMAGE_ENLARGE_BELOW_EDGE=200, **ON)
    def test_the_line_can_be_moved(self):
        self.assertEqual(enlargement_scale(199, 300), 4)
        self.assertEqual(enlargement_scale(200, 300), 0)

    @override_settings(SMALL_IMAGE_ENLARGE_BELOW_EDGE=480, SMALL_IMAGE_ENLARGE_TARGET_EDGE=480, **ON)
    def test_two_times_is_enough_when_it_reaches_the_target(self):
        self.assertEqual(enlargement_scale(300, 400), 2)
        self.assertEqual(enlargement_scale(200, 400), 4)

    @override_settings(PREPROCESS_MAX_WIDTH=500, PREPROCESS_MAX_HEIGHT=500, **ON)
    def test_it_never_goes_past_the_preprocessing_size_limit(self):
        self.assertEqual(enlargement_scale(150, 200), 2)        # x4 would be 600 x 800
        with override_settings(PREPROCESS_MAX_WIDTH=300, PREPROCESS_MAX_HEIGHT=300):
            self.assertEqual(enlargement_scale(150, 200), 0)    # not even x2 fits (300 x 400)


@override_settings(**ON)
class EnlargeTests(SimpleTestCase):
    @override_settings(SMALL_IMAGE_ENLARGE_ENGINE="opencv")
    def test_opencv_enlarges_by_the_planned_factor_without_touching_the_source(self):
        source = small_picture()
        before = np.asarray(source).copy()
        result, info = enlarge_small_image(source)
        self.assertEqual(result.size, (600, 800))
        self.assertEqual(result.mode, "RGB")
        np.testing.assert_array_equal(np.asarray(source), before)
        self.assertEqual((info["applied"], info["scale"], info["engine"], info["from"], info["to"]), (True, 4, OPENCV_ENGINE, [150, 200], [600, 800]))
        again, _ = enlarge_small_image(source)
        np.testing.assert_array_equal(np.asarray(again), np.asarray(result))      # deterministic

    @override_settings(SMALL_IMAGE_ENLARGE_ENGINE="opencv")
    def test_transparency_is_kept(self):
        result, info = enlarge_small_image(small_picture(mode="RGBA"))
        self.assertEqual((result.mode, result.size), ("RGBA", (600, 800)))
        alpha = np.asarray(result.getchannel("A"))
        self.assertLess(int(alpha[:, :100].max()), 10)           # the transparent half is still transparent
        self.assertGreater(int(alpha[:, -100:].min()), 245)      # and the opaque half still opaque
        self.assertTrue(info["applied"])

    def test_a_photograph_that_is_not_small_is_returned_as_it_is(self):
        big = Image.new("RGB", (600, 500), "white")
        result, info = enlarge_small_image(big)
        self.assertIs(result, big)
        self.assertEqual(info, {})

    @override_settings(SMALL_IMAGE_ENLARGE_ENABLED=False)
    def test_with_the_setting_off_nothing_happens(self):
        small = small_picture()
        result, info = enlarge_small_image(small)
        self.assertIs(result, small)
        self.assertEqual(info, {})

    @override_settings(HD_RESTORATION_ENABLED=False)
    def test_auto_uses_opencv_when_hd_restoration_is_off(self):
        result, info = enlarge_small_image(small_picture())
        self.assertEqual((info["applied"], info["engine"]), (True, OPENCV_ENGINE))
        self.assertIn("HD_RESTORATION_ENABLED", info["fallback_reason"])
        self.assertEqual(result.size, (600, 800))

    @override_settings(HD_RESTORATION_ENABLED=True, REALESRGAN_OUTPUT_BLEND=0.35)
    @patch("enhancement.hd_restoration.upscale_with_realesrgan", side_effect=fake_model)
    def test_auto_uses_the_model_when_it_is_on_and_blends_it_like_the_enhancement_engine(self, model):
        result, info = enlarge_small_image(small_picture())
        model.assert_called_once()
        self.assertEqual(model.call_args.kwargs["output_scale"], 4)
        self.assertEqual((info["applied"], info["engine"], info["fallback_reason"]), (True, REALESRGAN_ENGINE, ""))
        self.assertEqual(result.size, (600, 800))

    @override_settings(HD_RESTORATION_ENABLED=True)
    def test_a_model_that_cannot_run_falls_back_to_opencv_and_says_why(self):
        from enhancement.hd_restoration import HDRestorationUnavailable

        with patch("enhancement.hd_restoration.upscale_with_realesrgan", side_effect=HDRestorationUnavailable("The Real-ESRGAN checkpoint is unavailable.")):
            result, info = enlarge_small_image(small_picture())
        self.assertEqual((info["applied"], info["engine"]), (True, OPENCV_ENGINE))
        self.assertIn("checkpoint is unavailable", info["fallback_reason"])
        self.assertEqual(result.size, (600, 800))

    @override_settings(HD_RESTORATION_ENABLED=True, SMALL_IMAGE_ENLARGE_ENGINE="realesrgan")
    def test_the_strict_model_setting_never_silently_uses_opencv(self):
        small = small_picture()
        with patch("enhancement.hd_restoration.upscale_with_realesrgan", side_effect=RuntimeError("no GPU")):
            result, info = enlarge_small_image(small)
        self.assertIs(result, small)
        self.assertFalse(info["applied"])
        self.assertIn("no GPU", info["reason"])

    @override_settings(SMALL_IMAGE_ENLARGE_ENGINE="realesrgan", HD_RESTORATION_ENABLED=False)
    def test_the_strict_model_setting_respects_the_hd_switch(self):
        small = small_picture()
        result, info = enlarge_small_image(small)
        self.assertIs(result, small)
        self.assertIn("HD_RESTORATION_ENABLED", info["reason"])

    @override_settings(HD_RESTORATION_ENABLED=True)
    def test_a_model_result_that_no_longer_looks_like_the_input_is_not_used(self):
        def garbage(bgr, output_scale=None):
            scale = int(output_scale)
            noise = np.random.default_rng(1).integers(0, 255, (bgr.shape[0] * scale, bgr.shape[1] * scale, 3), dtype=np.uint8)
            return noise, {}

        with patch("enhancement.hd_restoration.upscale_with_realesrgan", side_effect=garbage):
            result, info = enlarge_small_image(small_picture())
        self.assertEqual(info["engine"], OPENCV_ENGINE)
        self.assertIn("does not match the input", info["fallback_reason"])

    @override_settings(HD_RESTORATION_ENABLED=True)
    def test_a_model_result_of_the_wrong_size_is_not_used(self):
        with patch("enhancement.hd_restoration.upscale_with_realesrgan", side_effect=lambda bgr, output_scale=None: (bgr, {})):
            result, info = enlarge_small_image(small_picture())
        self.assertEqual((info["engine"], result.size), (OPENCV_ENGINE, (600, 800)))
        self.assertIn("unexpected size", info["fallback_reason"])

    def test_it_never_raises(self):
        small = small_picture()
        with patch("uploads.enlarge._enlarge_bgr", side_effect=RuntimeError("boom")):
            result, info = enlarge_small_image(small)
        self.assertIs(result, small)
        self.assertEqual(info["applied"], False)
        self.assertIn("boom", info["reason"])

    @skipUnless(os.environ.get("REALESRGAN_MODEL_PATH") and os.path.isfile(os.environ.get("REALESRGAN_MODEL_PATH", "")),
                "needs the Real-ESRGAN checkpoint (REALESRGAN_MODEL_PATH) and PyTorch")
    @override_settings(HD_RESTORATION_ENABLED=True)
    def test_the_real_model_enlarges_a_small_picture(self):
        result, info = enlarge_small_image(small_picture())
        self.assertEqual((info["applied"], info["engine"], result.size), (True, REALESRGAN_ENGINE, (600, 800)))


@override_settings(QUALITY_CHECKS_REJECT=True)      # these tests are about refusing a photograph; the product default no longer refuses (test_never_reject.py)
class PreprocessingTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media.name, DUPLICATE_AUTO_ANALYZE=False, IDENTITY_AUTO_ANALYZE=False, AUTO_ENHANCEMENT_ENABLED=False,
            CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True, SMALL_IMAGE_ENLARGE_ENGINE="opencv",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = User.objects.create_user(username="small", email="small@example.com", password="ForestRiver!4821", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")

    def upload(self, width=150, height=200):
        payload = png_bytes(small_picture(width, height))
        image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="small.png", file_extension=".png", mime_type="image/png",
            file_size=len(payload), width=width, height=height, status="uploaded",
        )
        image.original_file.save("small.png", ContentFile(payload))
        return image, hashlib.sha256(payload).hexdigest()

    def processed_size(self, result):
        with result.processed_file.open("rb") as handle:
            return Image.open(io.BytesIO(handle.read())).size

    def run_preprocessing(self, image):
        with self.captureOnCommitCallbacks(execute=True):
            result, _created = preprocess_image(image, requested_format="png")
        return result

    @override_settings(**ON)
    def test_a_small_upload_is_standardized_at_the_larger_size_and_the_original_is_untouched(self):
        image, digest = self.upload()
        result = self.run_preprocessing(image)
        self.assertEqual((result.original_width, result.original_height), (150, 200))
        self.assertEqual((result.output_width, result.output_height), (600, 800))
        self.assertEqual(self.processed_size(result), (600, 800))
        self.assertEqual((result.enlargement["applied"], result.enlargement["engine"], result.enlargement["scale"]), (True, OPENCV_ENGINE, 4))
        with image.original_file.open("rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), digest)
        self.assertEqual((image.width, image.height), (150, 200))

    @override_settings(SMALL_IMAGE_ENLARGE_ENABLED=False)
    def test_with_the_setting_off_the_standardized_copy_has_the_original_size(self):
        image, _digest = self.upload()
        result = self.run_preprocessing(image)
        self.assertEqual((result.output_width, result.output_height), (150, 200))
        self.assertEqual(result.enlargement, {})

    @override_settings(**ON)
    def test_the_api_shows_what_was_done(self):
        image, _digest = self.upload()
        data = ImagePreprocessingResultSerializer(self.run_preprocessing(image)).data
        self.assertEqual((data["original_width"], data["output_width"]), (150, 600))
        self.assertEqual((data["enlargement"]["applied"], data["enlargement"]["from"], data["enlargement"]["to"]), (True, [150, 200], [600, 800]))

    @override_settings(**ON)
    def test_a_failure_to_enlarge_never_fails_the_upload(self):
        image, _digest = self.upload()
        with patch("uploads.enlarge._enlarge_bgr", side_effect=RuntimeError("boom")):
            result = self.run_preprocessing(image)
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.output_width, result.output_height), (150, 200))
        self.assertEqual(result.enlargement["applied"], False)

    def quality_reasons(self, image):
        image.refresh_from_db()
        decision = image.quality_decision
        decision.refresh_from_db()
        return decision.decision, list(decision.rejection_reasons), decision.measurements

    @override_settings(SMALL_IMAGE_ENLARGE_ENABLED=False)
    def test_without_the_setting_a_small_photograph_is_refused_for_its_size(self):
        image, _digest = self.upload()
        self.run_preprocessing(image)
        decision, reasons, _measurements = self.quality_reasons(image)
        self.assertEqual(decision, "rejected")
        self.assertIn("resolution_too_low", reasons)

    @override_settings(**ON)
    def test_with_the_setting_the_size_is_no_longer_a_reason_and_the_decision_says_it_was_enlarged(self):
        image, _digest = self.upload()
        self.run_preprocessing(image)
        _decision, reasons, measurements = self.quality_reasons(image)
        self.assertNotIn("resolution_too_low", reasons)
        self.assertEqual(measurements["enlarged_from"], [150, 200])
        self.assertEqual((measurements["width"], measurements["height"]), (600, 800))

    @override_settings(**ON)
    def test_a_photograph_that_was_not_enlarged_carries_no_enlarged_from(self):
        image, _digest = self.upload(width=700, height=800)
        self.run_preprocessing(image)
        _decision, _reasons, measurements = self.quality_reasons(image)
        self.assertNotIn("enlarged_from", measurements)

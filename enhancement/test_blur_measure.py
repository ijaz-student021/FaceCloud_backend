"""Blur verdicts that do not depend on resolution or exposure, used by every stage.

Classical processing only. The test target is a dead-leaves chart, the
scale-invariant pattern camera-sharpness standards use (ISO 19567-2): its edge
statistics are the same at every size, so a change of verdict with size is a
fault of the measure, not of the picture. Real photographs were studied
separately (docs/evaluation/2026-10-14-blur-measure.md).
"""
import cv2
import numpy as np
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .models import EnhancementLevel, EnhancementStyle
from .services import (
    BLUR_PROXY_LONG_EDGE,
    EnhancementPipelineContext,
    _build_processing_plan,
    analyze_image_quality,
    analyze_original_stage,
    hd_restoration_stage,
    measure_blur,
    restore_degraded_faces,
)
from .test_pipeline_regressions import NO_MODELS


def dead_leaves(size=1536, seed=7, colour=True):
    """Overlapping discs, radii ~ r^-3 between 2 px and 1/6 of the image."""
    rng = np.random.default_rng(seed)
    height, width = size * 2 // 3, size
    image = np.full((height, width, 3), 128, np.uint8)
    smallest, largest = 2.0, size / 6
    # Inverse-transform sampling of p(r) ~ r^-3 on [smallest, largest].
    uniform = rng.random(60000)
    radii = (smallest ** -2 - uniform * (smallest ** -2 - largest ** -2)) ** -0.5
    for radius in radii[::-1]:
        centre = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        shade = rng.integers(20, 236, 3) if colour else np.repeat(rng.integers(20, 236), 3)
        cv2.circle(image, centre, int(round(radius)), tuple(int(v) for v in shade), -1, lineType=cv2.LINE_AA)
    return image


def resized(image, long_edge):
    scale = long_edge / max(image.shape[:2])
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def darkened(image, gain):
    return np.clip(np.rint(image.astype(np.float32) * gain), 0, 255).astype(np.uint8)


def blurred(image, sigma_at_proxy):
    """Gaussian blur of a given size *as seen at the measuring scale*."""
    factor = max(1.0, max(image.shape[:2]) / BLUR_PROXY_LONG_EDGE)
    return cv2.GaussianBlur(image, (0, 0), sigma_at_proxy * factor)


def noisy(image, sigma, seed=3):
    noise = np.random.default_rng(seed).normal(0, sigma, image.shape)
    return np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def verdict(image):
    return measure_blur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))["verdict"]


class VerdictTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sharp = dead_leaves()

    def test_a_sharp_picture_reads_sharp_at_every_size(self):
        """P1-4: the old measure called the same sharp photo blurred once it had enough pixels."""
        for long_edge in (1536, 1200, 1024, 768, 512):
            with self.subTest(long_edge=long_edge):
                self.assertEqual(verdict(resized(self.sharp, long_edge)), "sharp")

    def test_a_sharp_picture_reads_sharp_when_dark(self):
        """P1-4: a 4x darkening used to read as severe blur."""
        for gain in (1.0, 0.5, 0.25):
            with self.subTest(gain=gain):
                self.assertEqual(verdict(darkened(self.sharp, gain)), "sharp")

    def test_noise_and_compression_do_not_change_a_sharp_verdict(self):
        self.assertEqual(verdict(noisy(self.sharp, 8)), "sharp")
        jpeg = cv2.imdecode(cv2.imencode(".jpg", self.sharp, [cv2.IMWRITE_JPEG_QUALITY, 30])[1], cv2.IMREAD_COLOR)
        self.assertEqual(verdict(jpeg), "sharp")

    def test_blur_is_found_whatever_the_size_or_exposure(self):
        for long_edge in (1536, 1024, 768):
            for gain in (1.0, 0.25):
                with self.subTest(long_edge=long_edge, gain=gain):
                    picture = darkened(resized(self.sharp, long_edge), gain)
                    # Darkness and noise can make a severe blur read as mild (the
                    # study measured this); it must never read as sharp.
                    self.assertIn(verdict(blurred(picture, 3.0)), {"mild", "severe"})
                    self.assertIn(verdict(blurred(picture, 1.3)), {"mild", "severe"})
        self.assertEqual(verdict(blurred(self.sharp, 3.0)), "severe")

    def test_noise_does_not_hide_blur(self):
        self.assertIn(verdict(noisy(blurred(self.sharp, 3.0), 8)), {"mild", "severe"})

    def test_a_frame_with_nothing_to_judge_is_uncertain_not_blurred(self):
        flat = np.full((600, 900, 3), 120, np.uint8)
        grain = np.random.default_rng(5).integers(0, 4, size=(600, 900, 3), dtype=np.uint8)
        self.assertEqual(verdict(flat), "uncertain")
        self.assertEqual(verdict(grain), "uncertain")

    def test_the_old_measure_is_still_reported_for_comparison(self):
        quality = analyze_image_quality(self.sharp)
        self.assertIn("laplacian_sharpness", quality)
        self.assertEqual(quality["blur"]["measure"], "edge_sharpness_v1")


@override_settings(**{**NO_MODELS, "HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True,
                      "GFPGAN_ENABLED": True, "GFPGAN_ALLOW_MILD_BLUR": True})
class OneVerdictForEveryStageTests(SimpleTestCase):
    """Sharpening, deblurring, face restoration, denoise protection and the plan
    all follow the same input verdict."""

    def routed(self, image):
        context = EnhancementPipelineContext(image, EnhancementLevel.AUTO, style=EnhancementStyle.NATURAL)
        analyze_original_stage(context)
        return context

    def eligible(self, context):
        """GFPGAN's eligible defects; the model itself must not be reached."""
        with patch("enhancement.services.restore_faces", side_effect=AssertionError("GFPGAN must not run")):
            return restore_degraded_faces(context.current_image, context.input_quality)[1]["eligible_defects"]

    def assert_no_blur_route(self, context):
        plan = context.plan["steps"]
        self.assertFalse(plan["sharpen"]["planned"])
        self.assertFalse(plan["deblur"]["planned"])
        self.assertFalse(plan["face_restoration"]["planned"])
        self.assertEqual(self.eligible(context), [])

    def test_a_sharp_night_photo_gets_no_blur_correction(self):
        context = self.routed(darkened(resized(dead_leaves(), 1200), 0.25))
        self.assertEqual(context.input_quality["blur"]["verdict"], "sharp")
        self.assert_no_blur_route(context)

    def test_a_sharp_high_resolution_photo_gets_no_blur_correction(self):
        context = self.routed(cv2.resize(dead_leaves(1536), None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
        self.assertEqual(context.input_quality["blur"]["verdict"], "sharp")
        self.assert_no_blur_route(context)

    def test_a_blurred_photo_is_routed_to_every_blur_stage(self):
        context = self.routed(blurred(resized(dead_leaves(), 1200), 3.0))
        self.assertEqual(context.input_quality["blur"]["verdict"], "severe")
        plan = context.plan["steps"]
        self.assertTrue(plan["sharpen"]["planned"])
        self.assertTrue(plan["deblur"]["planned"])
        self.assertTrue(plan["face_restoration"]["planned"])
        with patch("enhancement.services.restore_faces",
                   side_effect=lambda image: (image, {"faces_restored": 0})) as restore:
            _image, metadata = restore_degraded_faces(context.current_image, context.input_quality)
        self.assertEqual(metadata["eligible_defects"], ["severe_blur"])
        restore.assert_called_once()

    def test_the_deblur_plan_matches_what_the_stage_does(self):
        """P3-19: the plan used to promise deblurring whenever NAFNet was on."""
        sharp = self.routed(resized(dead_leaves(), 1200))
        self.assertFalse(sharp.plan["steps"]["deblur"]["planned"])
        with patch("enhancement.services.deblur_with_nafnet") as deblur, \
                patch("enhancement.services.upscale_with_realesrgan", side_effect=lambda image: (image, {})):
            hd_restoration_stage(sharp)
        deblur.assert_not_called()
        self.assertIn("No blur was measured", sharp.hd_restoration["deblur"]["fallback_reason"])


def blurred_photo(height=720, width=960, sigma=3.0):
    """A picture with real edges, blurred enough to read as severe blur.

    For tests that need "a blurred photo": a smooth gradient has no edges, so
    it says nothing about focus and the blur measure calls it uncertain.
    """
    scene = cv2.resize(dead_leaves(max(height, width) * 3 // 2), (width, height), interpolation=cv2.INTER_AREA)
    return blurred(scene, sigma)

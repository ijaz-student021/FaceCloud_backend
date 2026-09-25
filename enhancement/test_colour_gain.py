from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from .models import EnhancementLevel
from .services import (
    EnhancementPipelineContext,
    _amplify_source_chroma,
    _preserve_source_chroma,
    analyze_original_stage,
    exposure_enhancement_stage,
)


def mean_chroma(image):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    return float(np.mean(np.abs(lab[:, :, 1] - 128) + np.abs(lab[:, :, 2] - 128)))


def dark_tinted_photo(seed=4, size=(120, 160)):
    """A near-black photo with a faint, consistent warm tint and some texture."""
    rng = np.random.default_rng(seed)
    base = np.array([3, 5, 9], dtype=np.float32)
    photo = base + rng.normal(0, 0.8, (*size, 3))
    return np.clip(photo, 0, 255).astype(np.uint8)


def brightened(source, factor=12):
    return np.clip(source.astype(np.float32) * factor, 0, 255).astype(np.uint8)


class AmplifySourceChromaTests(SimpleTestCase):
    def setUp(self):
        self.source = dark_tinted_photo()
        self.protected = _preserve_source_chroma(self.source, brightened(self.source))

    def test_a_brightened_dark_photo_regains_some_of_its_own_colour(self):
        result, metadata = _amplify_source_chroma(self.source, self.protected, 9.0)
        self.assertTrue(metadata["applied"])
        self.assertGreater(mean_chroma(result), mean_chroma(self.protected) * 1.3)

    def test_the_hue_is_the_sources_own_never_a_new_one(self):
        result, _metadata = _amplify_source_chroma(self.source, self.protected, 9.0)
        lab_before = cv2.cvtColor(self.protected, cv2.COLOR_BGR2LAB).astype(np.float32) - 128
        lab_after = cv2.cvtColor(result, cv2.COLOR_BGR2LAB).astype(np.float32) - 128
        for channel in (1, 2):
            self.assertEqual(np.sign(lab_before[:, :, channel].mean()), np.sign(lab_after[:, :, channel].mean()))

    def test_the_boost_is_bounded_by_the_square_root_of_the_cap(self):
        _result, metadata = _amplify_source_chroma(self.source, self.protected, 9.0)
        self.assertLessEqual(metadata["mean_gain"], 3.0 + 0.01)
        _result, small = _amplify_source_chroma(self.source, self.protected, 4.0)
        self.assertLessEqual(small["mean_gain"], 2.0 + 0.01)

    def test_a_grey_photo_stays_grey(self):
        grey = np.full((90, 120, 3), 6, dtype=np.uint8)
        protected = _preserve_source_chroma(grey, brightened(grey))
        result, _metadata = _amplify_source_chroma(grey, protected, 9.0)
        self.assertLess(mean_chroma(result), 0.6)

    def test_luminance_is_untouched(self):
        result, _metadata = _amplify_source_chroma(self.source, self.protected, 9.0)
        before = cv2.cvtColor(self.protected, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.int16)
        after = cv2.cvtColor(result, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.int16)
        self.assertLess(float(np.abs(before - after).mean()), 1.5)

    def test_no_brightening_means_no_change(self):
        flat = np.full((80, 100, 3), (60, 90, 140), dtype=np.uint8)
        result, metadata = _amplify_source_chroma(flat, flat.copy(), 9.0)
        self.assertLess(float(np.abs(result.astype(np.int16) - flat).max()), 2)
        self.assertAlmostEqual(metadata["mean_gain"], 1.0, places=2)

    def test_a_cap_of_one_or_less_switches_it_off(self):
        for cap in (1.0, 0.5, 0.0, -3.0):
            result, metadata = _amplify_source_chroma(self.source, self.protected, cap)
            self.assertIs(result, self.protected)
            self.assertFalse(metadata["applied"])

    def test_odd_and_tiny_sizes_keep_shape_and_type(self):
        for shape in ((1, 1), (3, 200), (37, 53), (700, 5)):
            source = dark_tinted_photo(size=shape)
            protected = _preserve_source_chroma(source, brightened(source))
            result, _metadata = _amplify_source_chroma(source, protected, 9.0)
            self.assertEqual(result.shape, source.shape)
            self.assertEqual(result.dtype, np.uint8)


@override_settings(ZERO_DCE_ENABLED=True, AI_RESTORATION_ENABLED=False)
class ExposureStageColourGainTests(SimpleTestCase):
    def run_exposure_stage(self, cap):
        source = dark_tinted_photo(size=(160, 200))
        context = EnhancementPipelineContext(
            source, EnhancementLevel.HIGH, style="natural",
            allow_biometric_processing=True, use_zero_dce=True,
        )
        analyze_original_stage(context)
        self.assertTrue(context.zero_dce["requested"])
        with override_settings(ENHANCEMENT_DARK_COLOUR_GAIN_MAX=cap), patch(
            "enhancement.services.enhance_with_zero_dce", return_value=(brightened(source), {}),
        ):
            exposure_enhancement_stage(context)
        return source, context

    def test_the_gain_is_recorded_and_applied_after_the_learned_brightening(self):
        source, context = self.run_exposure_stage(9.0)
        gain = context.zero_dce["chroma_gain"]
        self.assertTrue(gain["applied"])
        self.assertGreater(gain["mean_gain"], 1.5)
        self.assertEqual(context.zero_dce["chroma_protection"], "preserve_source_lab_chroma")
        protected = _preserve_source_chroma(source, brightened(source))
        self.assertGreater(mean_chroma(context.current_image), mean_chroma(protected))

    def test_switched_off_the_output_is_exactly_the_old_behaviour(self):
        source, context = self.run_exposure_stage(1.0)
        self.assertFalse(context.zero_dce["chroma_gain"]["applied"])
        protected = _preserve_source_chroma(source, brightened(source))
        self.assertTrue(np.array_equal(context.current_image, protected))

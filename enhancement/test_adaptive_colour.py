"""The colour handling of a low-light lift (mocked network luminance; the real network is measured by
scripts/evaluate_lowlight_colour.py and enhancement/test_adaptive_real_models.py)."""
import cv2
import numpy as np
from django.test import SimpleTestCase

from enhancement.adaptive.colour import chromaticity_linear, from_linear, lift_colour_metrics, luminance_linear, to_linear


def scene():
    """A synthetic scene of saturated patches (brown, red, green, blue, skin) with smooth shading."""
    image = np.zeros((240, 320, 3), np.uint8)
    colours = [(30, 60, 110), (40, 40, 200), (60, 180, 60), (200, 80, 40), (120, 150, 210), (90, 110, 140)]
    for index, colour in enumerate(colours):
        image[:, index * 53:(index + 1) * 53 + 1] = colour
    gradient = np.linspace(0.8, 1.15, 320, dtype=np.float32)[None, :, None]
    return np.clip(image.astype(np.float32) * gradient, 0, 255).astype(np.uint8)


def chroma_median(image):
    lab = cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    return float(np.median(np.hypot(lab[..., 1], lab[..., 2])))


class ChromaticityLinearTests(SimpleTestCase):
    def test_brightening_in_linear_light_recovers_the_original_colours(self):
        clean = scene()
        dark = from_linear(to_linear(clean) / 8.0)
        # the "network": the right luminance, but grey (it lost all colour, as a pale output does)
        luminance = luminance_linear(to_linear(clean))
        grey = from_linear(np.repeat(luminance[..., None], 3, axis=2))
        result = chromaticity_linear(dark, grey)
        lab_clean = cv2.cvtColor(clean.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
        lab_result = cv2.cvtColor(result.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
        interior = (slice(20, -20), slice(10, -10))
        error = np.hypot(lab_clean[..., 1] - lab_result[..., 1], lab_clean[..., 2] - lab_result[..., 2])[interior]
        self.assertLess(float(np.median(error)), 4.0)
        self.assertGreater(chroma_median(result), 0.85 * chroma_median(clean))   # not pale

    def test_the_network_luminance_is_what_sets_brightness(self):
        clean = scene()
        dark = from_linear(to_linear(clean) / 8.0)
        bright = from_linear(to_linear(clean))
        result = chromaticity_linear(dark, bright)
        self.assertAlmostEqual(float(result.mean()), float(bright.mean()), delta=6.0)

    def test_output_stays_in_range_and_keeps_shape(self):
        dark = from_linear(to_linear(scene()) / 20.0)
        over = np.full_like(dark, 255)
        result = chromaticity_linear(dark, over)
        self.assertEqual(result.shape, dark.shape)
        self.assertEqual(result.dtype, np.uint8)

    def test_a_saturated_highlight_is_scaled_not_clipped_into_another_hue(self):
        dark = np.full((64, 64, 3), (2, 6, 40), np.uint8)       # a very red patch
        result = chromaticity_linear(dark, np.full((64, 64, 3), 250, np.uint8))
        b, g, r = (float(result[..., c].mean()) for c in range(3))
        self.assertGreater(r, g)
        self.assertGreater(g, b)


class LiftColourMetricsTests(SimpleTestCase):
    def test_a_lift_that_keeps_colour_reads_as_one(self):
        clean = scene()
        dark = from_linear(to_linear(clean) / 8.0)
        metrics = lift_colour_metrics(dark, clean)
        self.assertLess(metrics["hue_shift"], 6.0)
        self.assertAlmostEqual(metrics["saturation_ratio"], 1.0, delta=0.25)

    def test_a_pale_lift_is_visible(self):
        clean = scene()
        dark = from_linear(to_linear(clean) / 8.0)
        grey = cv2.cvtColor(cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        pale = cv2.addWeighted(clean, 0.3, grey, 0.7, 0)
        self.assertLess(lift_colour_metrics(dark, pale)["saturation_ratio"], 0.6)

    def test_a_hue_rotation_is_visible(self):
        clean = scene()
        dark = from_linear(to_linear(clean) / 8.0)
        hsv = cv2.cvtColor(clean, cv2.COLOR_BGR2HSV)
        hsv[..., 0] = (hsv[..., 0].astype(np.int16) + 15) % 180      # 30 degrees
        self.assertGreater(lift_colour_metrics(dark, cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR))["hue_shift"], 15.0)

    def test_a_grey_scene_has_nothing_to_judge(self):
        grey = np.full((120, 160, 3), 90, np.uint8)
        metrics = lift_colour_metrics(grey // 4, grey)
        self.assertIsNone(metrics["hue_shift"])
        self.assertIsNone(metrics["saturation_ratio"])

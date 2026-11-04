"""Auto for the gallery editor reads the photograph's nature first (enhancement/auto_edit.py).

The photographs here are synthetic: a lit, warm subject on a flat dark background, which is the case the recipe was calibrated on (the owner's hand edit of a portrait on a black background that the
automatic enhancement had lifted). What was and was not checked on real photographs is in docs/evaluation/2026-11-04-auto-edit.md.
"""
from unittest.mock import Mock

import numpy as np
from django.test import SimpleTestCase

from . import auto_edit
from .auto_edit import ADAPTIVE_RANGE, classify_scene, measure_scene, recommend_adaptive_adjustments
from .controls import CONTROL_NAMES, recommend_local_adjustments
from .services import analyze_image_quality, apply_gallery_adjustments


def subject_on(background, size=(320, 240), subject=(70, 130, 215)):
    """A warm, shaded ellipse (about a quarter of the frame) on a flat background of grey level ``background`` (BGR, 8-bit)."""
    height, width = size
    image = np.full((height, width, 3), background, np.uint8)
    yy, xx = np.mgrid[0:height, 0:width]
    radial = np.clip(1.0 - (((xx - width / 2) / (width / 4)) ** 2 + ((yy - height / 2) / (height / 3)) ** 2), 0.0, 1.0)
    inside = radial > 0
    shaded = (np.array(subject, np.float32) * (0.55 + 0.45 * radial[:, :, None])).astype(np.uint8)
    image[inside] = shaded[inside]
    return image


def recommend(image, baseline=None):
    return recommend_adaptive_adjustments(image, analyze_image_quality(image), baseline=baseline)


def floor_of(image):
    return measure_scene(image)["dark_median"]


class RendererConstantsTests(SimpleTestCase):
    """The recipe solves the sliders with tone.py's constants; if the renderer changes, these fail instead of the recipe silently drifting."""

    def test_brightness_moves_the_lightness_by_the_constant_the_recipe_uses(self):
        from .tone import render_tone

        flat = np.full((60, 80, 3), 90, np.uint8)
        shifted = measure_scene(render_tone(flat, {"brightness": -50.0})[0])["p50"]      # a large step: the render is rounded to 8 bits, which is worth about 0.7 on this scale
        self.assertAlmostEqual((shifted - measure_scene(flat)["p50"]) / -50.0, auto_edit.BRIGHTNESS_K, delta=0.02)

    def test_contrast_scales_about_the_median_by_the_constant_the_recipe_uses(self):
        from .tone import render_tone

        ramp = np.repeat(np.linspace(40, 200, 200).astype(np.uint8)[None, :, None], 3, axis=2).repeat(40, axis=0)
        before, after = measure_scene(ramp), measure_scene(render_tone(ramp, {"contrast": 20.0})[0])
        self.assertAlmostEqual(((after["p95"] - after["p50"]) / (before["p95"] - before["p50"]) - 1.0) / 20.0, auto_edit.CONTRAST_K, delta=0.0004)


class ClassificationTests(SimpleTestCase):
    def test_a_lit_subject_on_a_dark_background_is_low_key(self):
        self.assertEqual(classify_scene(measure_scene(subject_on(24))), "low_key")

    def test_a_photograph_that_is_dark_everywhere_is_not_low_key(self):
        scene = measure_scene(np.full((240, 320, 3), 30, np.uint8))
        self.assertEqual(classify_scene(scene), "balanced")

    def test_a_bright_or_ordinary_photograph_is_balanced(self):
        self.assertEqual(classify_scene(measure_scene(np.full((240, 320, 3), 200, np.uint8))), "balanced")
        self.assertEqual(classify_scene(measure_scene(subject_on(150))), "balanced")

    def test_the_measurement_does_not_depend_on_the_size(self):
        small, large = measure_scene(subject_on(24, (320, 240))), measure_scene(subject_on(24, (1920, 1440)))
        for key in ("p50", "p95", "dark_share", "chroma_lit"):
            self.assertAlmostEqual(small[key], large[key], delta=max(1.5, 0.03 * abs(small[key])))


class LowKeyTests(SimpleTestCase):
    def setUp(self):
        self.original = subject_on(8)          # the upload: a deep black background
        self.lifted = subject_on(24)           # what the automatic enhancement made of it

    def test_a_floor_the_enhancement_lifted_is_put_back_with_the_renderers_own_formula(self):
        result = recommend(self.lifted, baseline=self.original)
        controls = result["controls"]
        floor, original_floor = floor_of(self.lifted), floor_of(self.original)
        expected = -(floor - auto_edit.LOW_KEY_BLACK_RATIO * original_floor) / 0.35      # the renderer's brightness constant (controls.py), written out on purpose
        self.assertEqual(result["nature"], "low_key")
        self.assertAlmostEqual(controls["brightness"], expected, delta=0.1)
        self.assertAlmostEqual(controls["shadows"], controls["brightness"] * auto_edit.LOW_KEY_SHADOWS_PER_BRIGHTNESS, delta=0.1)
        self.assertGreater(controls["contrast"], 0)
        self.assertGreater(controls["saturation"], 0)
        self.assertGreater(controls["warmth"], 0)
        self.assertTrue(any("lifted its black level" in line for line in result["rationale"]))
        self.assertNotIn("dark_photo_not_lifted", result["caution_flags"])

    def test_rendered_through_the_editor_the_floor_and_the_subject_reach_their_targets(self):
        lifted, original = subject_on(24, subject=(100, 160, 230)), subject_on(8, subject=(100, 160, 230))     # the target is inside the contrast range for this subject
        rendered, metadata = apply_gallery_adjustments(lifted, "auto", {"intent": "auto"}, baseline=original)
        before, after = measure_scene(lifted), measure_scene(rendered)
        self.assertEqual(metadata["auto"]["nature"], "low_key")
        self.assertEqual(metadata["safety_strength"], 1.0)
        self.assertAlmostEqual(after["dark_median"], auto_edit.LOW_KEY_BLACK_RATIO * floor_of(original), delta=2.0)
        self.assertLess(after["dark_median"], before["dark_median"] - 10)
        self.assertAlmostEqual(after["p95"], auto_edit.LOW_KEY_SUBJECT, delta=8.0)
        self.assertGreater(after["chroma_lit"], before["chroma_lit"])

    def test_without_the_unenhanced_photograph_the_exposure_is_left_alone(self):
        controls = recommend(self.lifted)["controls"]
        self.assertEqual((controls["brightness"], controls["shadows"]), (0.0, 0.0))
        self.assertTrue(any("was not available" in line for line in recommend(self.lifted)["rationale"]))

    def test_a_loader_that_fails_is_the_same_as_no_baseline(self):
        def broken():
            raise OSError("the file is gone")

        self.assertEqual(recommend(self.lifted, baseline=broken)["controls"]["brightness"], 0.0)

    def test_blacks_the_enhancement_did_not_lift_are_left_alone(self):
        result = recommend(self.original, baseline=self.original)
        self.assertEqual(result["controls"]["brightness"], 0.0)
        self.assertTrue(any("as photographed" in line for line in result["rationale"]))

    def test_a_naturally_dark_grey_background_is_not_crushed_to_black(self):
        grey = subject_on(30)
        self.assertEqual(recommend(grey, baseline=grey)["controls"]["brightness"], 0.0)

    def test_a_lift_within_the_deadband_is_not_touched(self):
        almost = subject_on(8 + 2)
        self.assertEqual(recommend(almost, baseline=self.original)["controls"]["brightness"], 0.0)

    def test_the_baseline_is_read_only_for_a_low_key_photograph(self):
        loader = Mock(return_value=self.original)
        recommend(subject_on(150), baseline=loader)
        loader.assert_not_called()
        recommend(self.lifted, baseline=loader)
        loader.assert_called_once()

    def test_a_subject_that_is_already_bright_gets_no_extra_contrast(self):
        bright = subject_on(8, subject=(170, 230, 255))
        self.assertEqual(recommend(bright, baseline=bright)["controls"]["contrast"], 0.0)

    def test_colour_is_only_enriched_where_the_lit_part_has_colour(self):
        grey_subject = subject_on(24, subject=(150, 150, 150))
        controls = recommend(grey_subject, baseline=subject_on(8, subject=(150, 150, 150)))["controls"]
        self.assertEqual((controls["saturation"], controls["warmth"]), (0.0, 0.0))

    def test_every_control_stays_inside_the_adaptive_range_and_the_model_sliders_stay_off(self):
        very_lifted = subject_on(120)
        controls = recommend(very_lifted, baseline=subject_on(2))["controls"]
        for name in CONTROL_NAMES:
            self.assertGreaterEqual(controls[name], ADAPTIVE_RANGE[name][0])
            self.assertLessEqual(controls[name], ADAPTIVE_RANGE[name][1])
        self.assertEqual((controls["denoise"], controls["deblur"], controls["face_restore"], controls["lowlight"]), (0.0, 0.0, 0.0, 0.0))


class OrdinaryPhotographTests(SimpleTestCase):
    def test_a_photograph_that_is_not_low_key_gets_what_it_got_before(self):
        image = subject_on(150)
        quality = analyze_image_quality(image)
        self.assertEqual(recommend_adaptive_adjustments(image, quality)["controls"], recommend_local_adjustments(quality, "auto")["controls"])

    def test_a_photograph_that_is_dark_everywhere_is_still_not_lifted(self):
        image = np.full((240, 320, 3), 30, np.uint8)
        result = recommend(image)
        self.assertEqual((result["controls"]["brightness"], result["controls"]["shadows"]), (0.0, 0.0))
        self.assertIn("dark_photo_not_lifted", result["caution_flags"])

    def test_coloured_photographs_that_are_not_low_key_are_not_re_coloured(self):
        for subject in ((125, 130, 140), (0, 90, 255), (128, 128, 128)):
            image = subject_on(150, subject=subject)
            quality = analyze_image_quality(image)
            self.assertEqual(recommend_adaptive_adjustments(image, quality)["controls"], recommend_local_adjustments(quality, "auto")["controls"])

    def test_the_other_intents_keep_the_earlier_recipe(self):
        image = subject_on(24)
        for intent in ("preserve", "brighten"):
            _out, metadata = apply_gallery_adjustments(image, "auto", {"intent": intent}, baseline=subject_on(8))
            self.assertNotIn("nature", metadata["auto"])
            self.assertEqual(metadata["auto"]["intent"], intent)
            self.assertEqual(metadata["requested"], recommend_local_adjustments(analyze_image_quality(image), intent)["controls"])

    def test_the_result_says_what_the_nature_was(self):
        _out, metadata = apply_gallery_adjustments(subject_on(150), "auto", {"intent": "auto"})
        self.assertEqual(metadata["auto"]["nature"], "balanced")
        self.assertIn("p95", metadata["auto"]["scene"])

"""The editing control contract: ranges, validation, neutral, rendering, the local recipe.

Classical processing only (no model, no network). Expected values come from
the documented semantics in enhancement/controls.py, from Pillow's own
ImageEnhance, and from properties (identity, commutation with downscaling).
"""
import json

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings
from PIL import Image, ImageEnhance
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from . import controls
from .controls import (
    CONTROL_NAMES, CONTROLS, INTENSITY, clamp_to_auto_range, neutral_settings,
    pillow_factors, recommend_local_adjustments, validate_controls,
)
from .serializers import GalleryAdjustmentSerializer
from .services import analyze_image_quality, apply_gallery_adjustments
from .test_blur_measure import blurred_photo, darkened, dead_leaves, noisy, resized
from .test_pipeline_regressions import NO_MODELS, ResultFixture
from .tone import SHARPNESS_MIN_SIGMA, render_tone, sharpness_sigma


def photo(long_edge=640):
    return resized(dead_leaves(), long_edge)


class ContractTests(SimpleTestCase):
    def test_every_control_is_defined_once_with_ranges_that_nest(self):
        self.assertEqual(CONTROL_NAMES, ("brightness", "contrast", "shadows", "highlights", "saturation", "warmth", "sharpness"))
        for control in CONTROLS.values():
            with self.subTest(control=control.name):
                self.assertEqual(control.neutral, 0.0)
                self.assertLessEqual(control.manual[0], control.auto[0])
                self.assertGreaterEqual(control.manual[1], control.auto[1])
                self.assertTrue(control.manual[0] <= control.neutral <= control.manual[1])
                self.assertTrue(control.description)

    def test_automatic_ranges_are_much_smaller_than_manual_ones(self):
        for control in CONTROLS.values():
            manual = control.manual[1] - control.manual[0]
            automatic = control.auto[1] - control.auto[0]
            self.assertLessEqual(automatic, manual * 0.2, control.name)

    def test_the_serializer_fields_come_from_the_contract(self):
        fields = GalleryAdjustmentSerializer().fields
        self.assertEqual(set(fields), set(CONTROL_NAMES) | {"intensity"})
        for name, control in (*CONTROLS.items(), ("intensity", INTENSITY)):
            self.assertEqual((fields[name].min_value, fields[name].max_value), control.manual, name)
            self.assertEqual(fields[name].default, control.neutral, name)

    def test_reset_is_neutral_and_a_separate_thing_from_auto(self):
        reset = neutral_settings()
        self.assertEqual({name: reset[name] for name in CONTROL_NAMES}, {name: 0.0 for name in CONTROL_NAMES})
        self.assertEqual(reset["intensity"], 100.0)
        self.assertEqual(validate_controls(reset), reset)
        self.assertEqual(validate_controls({}), reset)  # omitted means neutral, never an automatic recipe

    def test_pillow_factors_use_1_as_neutral_and_have_none_where_pillow_has_no_equivalent(self):
        neutral = pillow_factors(neutral_settings())
        self.assertEqual({name: neutral[name] for name in ("brightness", "contrast", "saturation", "sharpness")},
                         {"brightness": 1.0, "contrast": 1.0, "saturation": 1.0, "sharpness": 1.0})
        self.assertTrue(all(neutral[name] is None for name in ("shadows", "highlights", "warmth")))
        self.assertAlmostEqual(pillow_factors({"contrast": 50})["contrast"], 1.25)
        self.assertAlmostEqual(pillow_factors({"sharpness": -100})["sharpness"], 0.0)
        self.assertAlmostEqual(pillow_factors({"sharpness": 100})["sharpness"], 2.0)


class ValidationTests(SimpleTestCase):
    def errors(self, payload):
        with self.assertRaises(ValidationError) as raised:
            validate_controls(payload)
        return raised.exception.detail

    def test_valid_values_at_the_limits_are_accepted(self):
        for name, control in CONTROLS.items():
            for value in (control.manual[0], control.manual[1], 0, 12.5):
                self.assertEqual(validate_controls({name: value})[name], float(value))

    def test_out_of_range_values_name_the_field(self):
        for name, control in (*CONTROLS.items(), ("intensity", INTENSITY)):
            for value in (control.manual[0] - 0.01, control.manual[1] + 0.01, 1e9, -1e9):
                with self.subTest(control=name, value=value):
                    self.assertIn(name, self.errors({name: value}))

    def test_non_finite_numbers_are_refused_not_coerced(self):
        for value in (float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", "-Infinity"):
            with self.subTest(value=value):
                self.assertIn("brightness", self.errors({"brightness": value}))

    def test_booleans_and_malformed_values_are_refused(self):
        for value in (True, False, "", "abc", "12px", [1], {"a": 1}, None, "1e999"):
            with self.subTest(value=value):
                self.assertIn("brightness", self.errors({"brightness": value}))

    def test_a_misspelt_or_unknown_control_is_an_error_not_ignored(self):
        errors = self.errors({"brightnes": 10, "exposure": 5})
        self.assertEqual(set(errors), {"brightnes", "exposure"})
        self.assertIn("not supported", str(errors["brightnes"]))

    def test_a_non_object_is_refused(self):
        for payload in ([1, 2], "brightness", 5):
            self.assertIn("non_field_errors", self.errors(payload))

    def test_every_bad_field_is_reported_together(self):
        errors = self.errors({"brightness": 500, "contrast": "x", "warmth": float("nan")})
        self.assertEqual(set(errors), {"brightness", "contrast", "warmth"})

    def test_automatic_suggestions_are_clamped_to_the_automatic_range(self):
        clamped = clamp_to_auto_range({"brightness": 80, "contrast": -50, "highlights": 30, "sharpness": -40, "shadows": -9})
        self.assertEqual(clamped["brightness"], 16.0)
        self.assertEqual(clamped["contrast"], -8.0)
        self.assertEqual(clamped["highlights"], 0.0)  # automatic highlights only recover
        self.assertEqual(clamped["sharpness"], 0.0)   # automatic sharpness never softens
        self.assertEqual(clamped["shadows"], 0.0)


class RendererTests(SimpleTestCase):
    def test_neutral_controls_return_the_source_exactly(self):
        source = photo()
        output, effective = render_tone(source, neutral_settings())
        np.testing.assert_array_equal(output, source)
        self.assertIsNot(output, source)  # a copy, never the caller's array
        self.assertEqual(set(effective.values()), {0.0})

    def test_a_vanishing_control_does_not_requantise_the_photo(self):
        """The earlier 8-bit LAB round trip altered 38 % of real pixels, even for 'nothing'."""
        source = photo()
        output, _ = render_tone(source, {"brightness": 0.001})
        self.assertLess(float(np.abs(output.astype(int) - source.astype(int)).mean()), 0.01)

    def test_each_control_moves_the_photo_the_documented_way(self):
        source = photo()
        lab = lambda image: cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(float)  # noqa: E731
        base = lab(source)
        bright = lab(render_tone(source, {"brightness": 40})[0])
        self.assertAlmostEqual(float((bright[..., 0] - base[..., 0]).mean()), 40 * 0.35, delta=1.5)
        self.assertGreater(lab(render_tone(source, {"contrast": 40})[0])[..., 0].std(), base[..., 0].std())
        self.assertLess(lab(render_tone(source, {"saturation": -60})[0])[..., 1:].std(),  base[..., 1:].std())
        self.assertGreater(lab(render_tone(source, {"warmth": 40})[0])[..., 2].mean(), base[..., 2].mean())
        lifted = lab(render_tone(source, {"shadows": 60})[0])[..., 0]
        dark, light = base[..., 0] < 60, base[..., 0] > 200
        self.assertGreater(float((lifted - base[..., 0])[dark].mean()), float((lifted - base[..., 0])[light].mean()))
        recovered = lab(render_tone(source, {"highlights": -60})[0])[..., 0]
        self.assertLess(float((recovered - base[..., 0])[light].mean()), -1.0)

    def test_sharpness_signs(self):
        source = photo()
        detail = lambda image: float(cv2.Laplacian(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())  # noqa: E731
        self.assertGreater(detail(render_tone(source, {"sharpness": 60})[0]), detail(source))
        self.assertLess(detail(render_tone(source, {"sharpness": -60})[0]), detail(source))

    def test_controls_are_applied_in_one_pass_not_stacked(self):
        """Two renders of half strength differ from one of full strength only by rounding and clipping."""
        source = photo()
        once = render_tone(source, {"brightness": 20, "contrast": 20})[0]
        halves = render_tone(render_tone(source, {"brightness": 10, "contrast": 10})[0], {"brightness": 10, "contrast": 10})[0]
        # Two half passes re-centre contrast on a new median, so they are not equal: the renderer offers one pass.
        self.assertEqual(once.shape, halves.shape)
        self.assertGreater(int(np.abs(once.astype(int) - halves.astype(int)).max()), 0)
        output, effective = render_tone(source, {"brightness": 20, "contrast": 20}, strength=0.5)
        np.testing.assert_array_equal(output, render_tone(source, {"brightness": 10, "contrast": 10})[0])
        self.assertEqual((effective["brightness"], effective["contrast"]), (10.0, 10.0))

    def test_the_sharpness_radius_follows_the_image_size(self):
        self.assertAlmostEqual(sharpness_sigma((1024, 768)), 0.68, places=2)
        self.assertAlmostEqual(sharpness_sigma((6000, 4000)), 0.68 * 6000 / 1024, places=2)
        self.assertEqual(sharpness_sigma((200, 100)), SHARPNESS_MIN_SIGMA)

    def test_a_preview_and_the_full_resolution_render_look_alike(self):
        """Render-then-shrink and shrink-then-render agree, for every control (the Phase B preview contract)."""
        big = resized(dead_leaves(), 1600)
        small = resized(big, 400)
        for name, value in (("brightness", 30), ("contrast", 30), ("shadows", 40), ("highlights", -40),
                            ("saturation", 40), ("warmth", 30), ("sharpness", 50)):
            with self.subTest(control=name):
                full_then_shrink = resized(render_tone(big, {name: value})[0], 400)
                shrink_then_render = render_tone(small, {name: value})[0]
                difference = float(np.abs(full_then_shrink.astype(int) - shrink_then_render.astype(int)).mean())
                self.assertLess(difference, 1.5, f"{name}: mean difference {difference:.2f} levels")

    def test_agreement_with_pillow_is_close_for_the_controls_pillow_has(self):
        """At 1024 px, where the sharpness radius equals Pillow's SMOOTH kernel (sigma 0.68).

        Mean absolute differences in grey levels on a generated chart whose edges
        are softened like a photograph's (a hard-edged chart is a worst case:
        sharpness then differs by 2.6). 40 real photos gave 4.1 (brightness), 2.7
        (contrast), 0.7 (saturation) and 0.2 (sharpness). Brightness is additive
        in LAB but multiplicative in Pillow's RGB, hence its larger difference.
        """
        source = cv2.GaussianBlur(photo(1024), (0, 0), 1.5)
        tolerance = {"brightness": 4.5, "contrast": 6.0, "saturation": 4.0, "sharpness": 1.0}
        enhancers = {"brightness": ImageEnhance.Brightness, "contrast": ImageEnhance.Contrast,
                     "saturation": ImageEnhance.Color, "sharpness": ImageEnhance.Sharpness}
        pil = Image.fromarray(cv2.cvtColor(source, cv2.COLOR_BGR2RGB))
        for name, limit in tolerance.items():
            with self.subTest(control=name):
                value = 50 if name == "sharpness" else 30
                theirs = cv2.cvtColor(np.asarray(enhancers[name](pil).enhance(pillow_factors({name: value})[name])),
                                      cv2.COLOR_RGB2BGR)
                ours = render_tone(source, {name: value})[0]
                self.assertLess(float(np.abs(ours.astype(int) - theirs.astype(int)).mean()), limit)


class LocalRecipeTests(SimpleTestCase):
    def recipe(self, image, intent="auto", **kwargs):
        return recommend_local_adjustments(analyze_image_quality(image), intent, **kwargs)

    def test_a_normal_photo_gets_no_change(self):
        outcome = self.recipe(photo())
        self.assertEqual(outcome["action"], "no_change")
        self.assertEqual(set(outcome["controls"].values()), {0.0})
        self.assertTrue(outcome["rationale"])

    def test_a_dark_photo_is_not_lifted_unless_asked(self):
        dark = darkened(photo(), 0.3)
        auto = self.recipe(dark)
        self.assertEqual((auto["controls"]["brightness"], auto["controls"]["shadows"]), (0.0, 0.0))
        self.assertIn("dark_photo_not_lifted", auto["caution_flags"])
        asked = self.recipe(dark, "brighten")
        self.assertGreater(asked["controls"]["brightness"], 0.0)
        self.assertGreater(asked["controls"]["shadows"], 0.0)
        self.assertEqual(asked["action"], "apply")

    def test_automatic_mode_never_darkens_a_bright_photo(self):
        bright = np.clip(photo().astype(np.int16) + 60, 0, 255).astype(np.uint8)
        self.assertLessEqual(self.recipe(bright)["controls"]["brightness"], 0.0)
        self.assertEqual(self.recipe(bright)["controls"]["brightness"], 0.0)

    def test_blown_highlights_are_recovered_and_flat_photos_get_contrast(self):
        blown = photo().copy()
        blown[:, : blown.shape[1] // 3] = 255
        self.assertLess(self.recipe(blown)["controls"]["highlights"], 0.0)
        flat = np.full((400, 600, 3), 120, np.uint8) + np.random.default_rng(1).integers(0, 6, (400, 600, 3), dtype=np.uint8)
        self.assertGreater(self.recipe(flat)["controls"]["contrast"], 0.0)

    def test_preserve_leaves_tonality_alone_even_for_blown_highlights(self):
        blown = photo().copy()
        blown[:, : blown.shape[1] // 3] = 255
        controls = self.recipe(blown, "preserve")["controls"]
        for name in ("brightness", "contrast", "shadows", "highlights"):
            self.assertEqual(controls[name], 0.0, name)

    def test_a_soft_photo_gets_light_sharpening_that_a_noisy_or_blocky_one_does_not(self):
        soft = blurred_photo(480, 640, sigma=1.3)
        quality = analyze_image_quality(soft)
        self.assertIn(quality["blur"]["verdict"], {"mild", "severe"})
        sharpened = recommend_local_adjustments(quality)
        self.assertTrue(0.0 < sharpened["controls"]["sharpness"] <= CONTROLS["sharpness"].auto[1])
        blocky = recommend_local_adjustments(quality, blockiness_detected=True)
        self.assertEqual(blocky["controls"]["sharpness"], 0.0)
        self.assertIn("compression_artifacts_not_sharpened", blocky["caution_flags"])
        noisy_quality = {**quality, "defects": [*quality["defects"], "visible_noise"]}
        self.assertEqual(recommend_local_adjustments(noisy_quality)["controls"]["sharpness"], 0.0)

    def test_the_recipe_never_leaves_the_automatic_range(self):
        extremes = [photo(), darkened(photo(), 0.1), blurred_photo(480, 640, sigma=4.0), noisy(photo(), 40)]
        for image in extremes:
            for intent in ("auto", "brighten", "preserve"):
                controls = self.recipe(image, intent)["controls"]
                for name, value in controls.items():
                    low, high = CONTROLS[name].auto
                    self.assertTrue(low <= value <= high, f"{name}={value} ({intent})")

    def test_an_unknown_intent_is_refused(self):
        with self.assertRaises(ValueError):
            recommend_local_adjustments(analyze_image_quality(photo()), "enhance")


class GalleryRenderMetadataTests(SimpleTestCase):
    def test_a_neutral_manual_edit_changes_nothing_and_says_so(self):
        source = photo()
        output, metadata = apply_gallery_adjustments(source, "manual", {})
        np.testing.assert_array_equal(output, source)
        self.assertEqual(metadata["outcome"], "no_change")
        self.assertEqual(metadata["renderer_version"], controls.RENDERER_VERSION)
        self.assertEqual(metadata["render_policy"], "lightweight_v1")
        self.assertEqual(metadata["guard_reductions"], [])

    def test_requested_validated_and_applied_values_are_recorded(self):
        _out, metadata = apply_gallery_adjustments(photo(), "manual", {"brightness": 10, "sharpness": 20, "intensity": 100})
        self.assertEqual(metadata["requested"]["brightness"], 10.0)
        self.assertEqual(metadata["validated"]["intensity"], 100.0)
        self.assertEqual(metadata["applied"]["sharpness"], 20.0)
        self.assertEqual(metadata["outcome"], "rendered")

    def test_a_guard_reduction_is_recorded_with_the_reason_and_the_applied_value_is_smaller(self):
        bright = np.clip(photo().astype(np.int16) + 70, 0, 255).astype(np.uint8)
        _out, metadata = apply_gallery_adjustments(bright, "manual", {"brightness": 100, "highlights": 100})
        self.assertLess(metadata["safety_strength"], 1.0)
        self.assertTrue(metadata["guard_reductions"])
        self.assertIn("highlight_clipping_limited", metadata["safety_reasons"])
        self.assertLess(metadata["applied"]["brightness"], metadata["requested"]["brightness"])

    def test_intensity_without_a_baseline_is_a_recorded_fallback_not_a_silent_skip(self):
        _out, metadata = apply_gallery_adjustments(photo(), "manual", {"brightness": 10, "intensity": 40})
        self.assertFalse(metadata["intensity"]["applied"])
        self.assertIn("intensity_not_applied_no_unenhanced_baseline", metadata["fallback_reasons"])

    def test_intensity_keeps_its_blend_semantics(self):
        enhanced, baseline = photo(), darkened(photo(), 0.5)
        output, metadata = apply_gallery_adjustments(enhanced, "manual", {"intensity": 25}, baseline=baseline)
        expected = cv2.addWeighted(enhanced, 0.25, baseline, 0.75, 0)
        np.testing.assert_array_equal(output, expected)
        self.assertTrue(metadata["intensity"]["applied"])

    def test_auto_records_its_recommendation(self):
        _out, metadata = apply_gallery_adjustments(darkened(photo(), 0.3), "auto", {"intent": "auto"})
        # A darkened photo is also flat, so a little contrast is fair; the lift is what is withheld.
        self.assertEqual((metadata["applied"]["brightness"], metadata["applied"]["shadows"]), (0.0, 0.0))
        self.assertIn("dark_photo_not_lifted", metadata["auto"]["caution_flags"])
        lifted, metadata = apply_gallery_adjustments(darkened(photo(), 0.3), "auto", {"intent": "brighten"})
        self.assertGreater(float(lifted.mean()), float(darkened(photo(), 0.3).mean()))
        self.assertEqual(metadata["auto"]["intent"], "brighten")

    def test_manual_mode_validates_again_at_render_time(self):
        with self.assertRaises(ValidationError):
            apply_gallery_adjustments(photo(), "manual", {"brightness": 1000})


@override_settings(**NO_MODELS)
class EditApiTests(ResultFixture):
    """Through the real endpoints, serializer and task body (queue mocked)."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch

        from .services import process_enhancement_result

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.result = process_enhancement_result(self.make_result("edit.jpg", size=(240, 320)))
        self.url = f"/api/enhancements/results/{self.result.pk}/edits/"
        self.patch = patch("enhancement.views.process_gallery_edit.apply_async")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def post(self, body):
        return self.client.post(self.url, body, format="json")

    def test_invalid_manual_input_is_a_field_error(self):
        for body, field in (
            ({"mode": "manual", "adjustments": {"brightness": 101}}, "adjustments"),
            ({"mode": "manual", "adjustments": {"brightness": "x"}}, "adjustments"),
            ({"mode": "manual", "adjustments": {"bright": 5}}, "adjustments"),
            ({"mode": "manual", "adjustments": {"warmth": True}}, "adjustments"),
            ({"mode": "manual", "adjustments": [1]}, "adjustments"),
            ({"mode": "manual", "intent": "brighten"}, "intent"),
            ({"mode": "auto", "intent": "enhance"}, "intent"),
        ):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json())

    def test_json_nan_and_infinity_are_refused_at_the_door(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            response = self.client.post(self.url, f'{{"mode": "manual", "adjustments": {{"brightness": {token}}}}}',
                                        content_type="application/json")
            self.assertEqual(response.status_code, 400, token)

    def test_a_sharpness_edit_is_accepted_and_rendered(self):
        from .models import ImageEditVersion
        from .tasks import process_gallery_edit

        response = self.post({"mode": "manual", "adjustments": {"sharpness": 40, "contrast": 5}})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["requested_adjustments"]["sharpness"], 40.0)
        self.assertEqual(process_gallery_edit.run(response.json()["id"])["status"], "completed")
        metadata = ImageEditVersion.objects.get(pk=response.json()["id"]).quality_metadata
        self.assertEqual(metadata["applied"]["sharpness"], 40.0)
        self.assertEqual(metadata["renderer_version"], controls.RENDERER_VERSION)

    def test_a_reset_edit_is_a_no_change_not_an_automatic_one(self):
        from .models import ImageEditVersion
        from .tasks import process_gallery_edit

        response = self.post({"mode": "manual"})  # nothing sent: neutral
        self.assertEqual(response.status_code, 202)
        process_gallery_edit.run(response.json()["id"])
        edit = ImageEditVersion.objects.get(pk=response.json()["id"])
        self.assertEqual(edit.quality_metadata["outcome"], "no_change")
        self.assertEqual(set(edit.applied_adjustments.values()), {0.0})

    def test_auto_stores_its_intent_and_records_the_recipe(self):
        from .models import ImageEditVersion
        from .tasks import process_gallery_edit

        response = self.post({"mode": "auto", "intent": "preserve"})
        self.assertEqual(response.json()["requested_adjustments"], {"intent": "preserve"})
        process_gallery_edit.run(response.json()["id"])
        auto = ImageEditVersion.objects.get(pk=response.json()["id"]).quality_metadata["auto"]
        self.assertEqual((auto["intent"], auto["action"]), ("preserve", "no_change"))

    def test_the_configuration_endpoint_serves_the_contract(self):
        body = self.client.get("/api/enhancements/editor/configuration/").json()
        self.assertEqual(body["renderer_version"], controls.RENDERER_VERSION)
        self.assertEqual(body["render_policy"]["name"], "lightweight_v1")
        self.assertEqual(body["neutral_settings"], neutral_settings())
        self.assertEqual(body["intents"], ["auto", "preserve", "brighten"])
        self.assertEqual(json.loads(json.dumps(body["controls"])), json.loads(json.dumps(controls.contract())))
        self.assertEqual(body["controls"]["sharpness"]["auto_maximum"], 20.0)
        self.assertEqual(body["controls"]["contrast"]["pillow_enhancer"], "Contrast")
        self.assertIsNone(body["controls"]["warmth"]["pillow_enhancer"])

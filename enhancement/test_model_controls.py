"""The model-backed editor sliders (enhancement/model_controls.py), with stand-in models.

What is proved here: which engine each slider uses and what it falls back to, that a slider is a blend of
the model's candidate (0 = untouched, 100 = the candidate), that candidates are cached so only the stages
after a moved slider recompute, that the order is fixed, that the guards' strength scales every slider, and
what the API tells a client about each slider. The real models are exercised in test_model_set.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import controls as controls_module
from . import model_controls
from .lowlight import LowLightUnavailable
from .model_controls import apply_model_controls, engine_status
from .services import apply_gallery_adjustments
from .test_balanced_policy import dark_photo
from .test_pipeline_regressions import textured

OFF = dict(RETINEXFORMER_ENABLED=False, ZERO_DCE_ENABLED=False, NAFNET_DENOISE_ENABLED=False,
           NAFNET_DEBLUR_ENABLED=False, CODEFORMER_ENABLED=False)


def brighter(image):
    return cv2.add(image, 60), {"provider": "stand_in", "device": "cpu"}


class EngineChoiceTests(SimpleTestCase):
    def setUp(self):
        model_controls.clear_cache()
        self.addCleanup(model_controls.clear_cache)

    @override_settings(**OFF)
    def test_with_every_model_off_the_classical_engines_serve_and_face_restoration_is_unavailable(self):
        status = engine_status()
        self.assertEqual(status["lowlight"]["engine"], "opencv_gamma")
        self.assertEqual(status["denoise"]["engine"], "opencv_bilateral")
        self.assertEqual(status["deblur"]["engine"], "opencv_unsharp")
        self.assertIsNone(status["face_restore"]["engine"])
        self.assertFalse(status["face_restore"]["available"])
        self.assertFalse(any(item["uses_model"] for item in status.values()))

    @override_settings(**{**OFF, "RETINEXFORMER_ENABLED": True, "ZERO_DCE_ENABLED": True, "NAFNET_DENOISE_ENABLED": True,
                          "NAFNET_DEBLUR_ENABLED": True, "CODEFORMER_ENABLED": True})
    def test_with_every_model_on_each_slider_uses_its_model_and_names_a_fallback(self):
        status = engine_status()
        self.assertEqual([status[name]["engine"] for name in ("lowlight", "denoise", "deblur", "face_restore")],
                         ["retinexformer", "nafnet_sidd", "nafnet_gopro", "codeformer"])
        self.assertTrue(all(item["uses_model"] for item in status.values()))
        self.assertEqual(status["lowlight"]["fallback_engine"], "zero_dce")

    @override_settings(**{**OFF, "ZERO_DCE_ENABLED": True})
    def test_zero_dce_serves_low_light_when_retinexformer_is_off(self):
        self.assertEqual(engine_status()["lowlight"]["engine"], "zero_dce")


@override_settings(**OFF)
class BlendTests(SimpleTestCase):
    def setUp(self):
        model_controls.clear_cache()
        self.addCleanup(model_controls.clear_cache)

    def test_a_slider_at_zero_leaves_the_photo_untouched_and_runs_nothing(self):
        source = dark_photo()
        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": lambda image: (_ for _ in ()).throw(AssertionError("ran"))}):
            output, report, reasons = apply_model_controls(source, {"lowlight": 0.0})
        np.testing.assert_array_equal(output, source)
        self.assertEqual((report, reasons), ([], []))

    def test_a_slider_is_a_blend_of_the_models_candidate(self):
        source = dark_photo()
        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": brighter}):
            half, report, _ = apply_model_controls(source, {"lowlight": 50})
            full, _, _ = apply_model_controls(source, {"lowlight": 100})
        np.testing.assert_array_equal(full, cv2.add(source, 60))
        expected = cv2.addWeighted(cv2.add(source, 60), 0.5, source, 0.5, 0)
        np.testing.assert_array_equal(half, expected)
        self.assertEqual((report[0]["control"], report[0]["engine"], report[0]["blend"]), ("lowlight", "opencv_gamma", 0.5))

    def test_the_guards_strength_scales_every_slider(self):
        source = dark_photo()
        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": brighter}):
            scaled, report, _ = apply_model_controls(source, {"lowlight": 100}, strength=0.5)
        np.testing.assert_array_equal(scaled, cv2.addWeighted(cv2.add(source, 60), 0.5, source, 0.5, 0))
        self.assertEqual(report[0]["blend"], 0.5)

    def test_a_model_that_changes_the_image_size_is_rejected_and_the_photo_is_left_alone(self):
        source = dark_photo()
        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": lambda image: (cv2.resize(image, None, fx=2, fy=2), {})}):
            output, report, reasons = apply_model_controls(source, {"lowlight": 50})
        np.testing.assert_array_equal(output, source)
        self.assertFalse(report[0]["applied"])
        self.assertIn("must not change the image size", report[0]["skipped_engines"][0]["reason"])
        self.assertIn("lowlight_not_applied", reasons)

    def test_the_real_candidate_is_computed_once_and_reused_until_its_input_changes(self):
        source = dark_photo()
        calls = []

        def counting(image):
            calls.append(1)
            return brighter(image)

        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": counting}):
            apply_model_controls(source, {"lowlight": 30})
            _out, report, _ = apply_model_controls(source, {"lowlight": 80})
            self.assertEqual(len(calls), 1)  # moving the slider only changed the blend
            self.assertTrue(report[0]["cached"])
            apply_model_controls(cv2.add(source, 1), {"lowlight": 80})
            self.assertEqual(len(calls), 2)  # a different input is a different candidate

    def test_the_stages_run_in_a_fixed_order_and_each_sees_the_previous_result(self):
        source = dark_photo()
        seen = []

        def record(name, shift):
            def run(image):
                seen.append((name, int(image.mean())))
                return cv2.add(image, shift), {"provider": name}
            return run

        runners = {"opencv_gamma": record("lowlight", 40), "opencv_bilateral": record("denoise", 0), "opencv_unsharp": record("deblur", 0)}
        with patch.dict(model_controls._RUNNERS, runners):
            apply_model_controls(source, {"deblur": 100, "denoise": 100, "lowlight": 100})
        self.assertEqual([name for name, _mean in seen], ["lowlight", "denoise", "deblur"])
        self.assertGreater(seen[1][1], seen[0][1] + 30)  # denoise received the brightened image

    @override_settings(**{**OFF, "CODEFORMER_ENABLED": False})
    def test_a_slider_with_no_engine_is_reported_not_silently_ignored(self):
        output, report, reasons = apply_model_controls(textured(), {"face_restore": 70})
        np.testing.assert_array_equal(output, textured())
        self.assertFalse(report[0]["applied"])
        self.assertIn("face_restore_not_applied", reasons)

    @override_settings(**{**OFF, "RETINEXFORMER_ENABLED": True})
    def test_a_model_that_fails_falls_back_to_the_next_engine_and_says_so(self):
        source = dark_photo()
        runners = {"retinexformer": lambda image: (_ for _ in ()).throw(LowLightUnavailable("checkpoint unavailable")),
                   "opencv_gamma": brighter}
        with patch.dict(model_controls._RUNNERS, runners):
            output, report, reasons = apply_model_controls(source, {"lowlight": 100})
        self.assertEqual(report[0]["engine"], "opencv_gamma")
        self.assertEqual(report[0]["skipped_engines"][0]["engine"], "retinexformer")
        self.assertIn("lowlight_retinexformer_unavailable", reasons)
        self.assertGreater(float(output.mean()), float(source.mean()))

    def test_the_classical_engines_really_run(self):
        dark = dark_photo()
        lifted, _, _ = apply_model_controls(dark, {"lowlight": 100})
        self.assertGreater(float(lifted.mean()), float(dark.mean()) + 10)
        soft = cv2.GaussianBlur(textured(240, 320), (0, 0), 2.0)
        sharpened, _, _ = apply_model_controls(soft, {"deblur": 100})
        gray = lambda image: cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)  # noqa: E731
        self.assertGreater(cv2.Laplacian(gray(sharpened), cv2.CV_64F).var(), cv2.Laplacian(gray(soft), cv2.CV_64F).var())


@override_settings(**OFF)
class ThroughTheEditorTests(SimpleTestCase):
    def setUp(self):
        model_controls.clear_cache()
        self.addCleanup(model_controls.clear_cache)

    def test_a_manual_edit_applies_models_before_tone_and_records_it(self):
        source = dark_photo()
        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": brighter}):
            output, metadata = apply_gallery_adjustments(source, "manual", {"lowlight": 60, "contrast": 20})
        self.assertEqual(metadata["outcome"], "rendered")
        self.assertEqual(metadata["model_controls"][0]["engine"], "opencv_gamma")
        self.assertEqual(metadata["applied"]["lowlight"], 60.0)
        self.assertGreater(float(output.mean()), float(source.mean()))

    def test_a_slider_that_could_not_run_is_in_the_fallback_reasons(self):
        _out, metadata = apply_gallery_adjustments(textured(), "manual", {"face_restore": 80})
        self.assertIn("face_restore_not_applied", metadata["fallback_reasons"])

    def test_auto_mode_never_uses_a_model_slider(self):
        with patch.dict(model_controls._RUNNERS, {"opencv_gamma": lambda image: (_ for _ in ()).throw(AssertionError("ran"))}):
            _out, metadata = apply_gallery_adjustments(dark_photo(), "auto", {"intent": "brighten"})
        self.assertEqual(metadata["model_controls"], [])

    def test_the_model_sliders_are_manual_only_for_the_recipe_and_the_advisor(self):
        for name in model_controls.MODEL_CONTROL_NAMES:
            self.assertEqual(controls_module.CONTROLS[name].auto, (0.0, 0.0))
            self.assertEqual(controls_module.clamp_to_auto_range({name: 90})[name], 0.0)

    def test_the_contract_says_which_engine_serves_each_slider(self):
        contract = controls_module.contract()
        self.assertTrue(contract["lowlight"]["model_backed"])
        self.assertEqual(contract["lowlight"]["engine"], "opencv_gamma")
        self.assertTrue(contract["lowlight"]["available"])
        self.assertFalse(contract["face_restore"]["available"])
        self.assertFalse(contract["brightness"]["model_backed"])
        self.assertTrue(contract["brightness"]["available"])

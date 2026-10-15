"""The lightweight rendering policy: no learned stage runs, nothing falls back to one.

Every learned adapter's entry point is replaced by a function that fails the
test if it is called, and every learned setting is switched ON, so a stage can
only stay quiet because the policy keeps it off.
"""
from contextlib import ExitStack
from functools import lru_cache
from unittest.mock import patch

import cv2
from django.test import SimpleTestCase, override_settings

from . import policy
from .models import EnhancementLevel
from .services import enhance_bgr
from .test_blur_measure import blurred_photo, darkened, dead_leaves, resized

ALL_LEARNED_ON = {flag: True for flag in policy.LEARNED_STAGES}
ADAPTERS = (
    "enhancement.services.enhance_with_zero_dce",
    "enhancement.services.deblur_with_nafnet",
    "enhancement.services.upscale_with_realesrgan",
    "enhancement.services.restore_faces",
    "enhancement.services.enhance_with_retinexformer",
    "enhancement.services.denoise_with_nafnet",
    "enhancement.services.restore_faces_codeformer",
)


def forbid_every_learned_adapter():
    stack = ExitStack()
    mocks = [
        stack.enter_context(patch(name, side_effect=AssertionError(f"{name} must not run under lightweight_v1")))
        for name in ADAPTERS
    ]
    return stack, mocks


@lru_cache(maxsize=1)
def base_photo():
    """Generated once: drawing the chart is far slower than enhancing it."""
    return dead_leaves()


def gray_photo():
    return cv2.cvtColor(cv2.cvtColor(resized(base_photo(), 640), cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


PHOTOS = {
    "sharp": lambda: resized(base_photo(), 640),
    "dark": lambda: darkened(resized(base_photo(), 640), 0.25),
    "blurred": lambda: blurred_photo(480, 640),
    "low_resolution": lambda: resized(base_photo(), 320),
    "grayscale": gray_photo,
}


@override_settings(**ALL_LEARNED_ON, IDENTITY_GUARD_ENABLED=True)
class LightweightPolicyTests(SimpleTestCase):
    def test_the_default_policy_is_lightweight(self):
        from django.conf import settings

        self.assertEqual(settings.ENHANCEMENT_RENDER_POLICY, "lightweight_v1")
        self.assertEqual(policy.active_policy(), "lightweight_v1")

    def test_no_learned_adapter_runs_for_any_photo_level_or_style(self):
        stack, mocks = forbid_every_learned_adapter()
        with stack:
            for name, make in PHOTOS.items():
                for level in ("auto", "high"):
                    for style in ("natural", "ai_max"):
                        with self.subTest(photo=name, level=level, style=style):
                            _out, _level, parameters = enhance_bgr(make(), level, style=style)
                            self.assertEqual(parameters["render_policy"]["name"], "lightweight_v1")
                            self.assertEqual(parameters["algorithm"].split("+")[0], "opencv_adaptive_restoration_router_v2")
                            for part in ("private_gfpgan", "realesrgan", "nafnet", "zero_dce"):
                                self.assertNotIn(part, parameters["algorithm"])
        for mock in mocks:
            mock.assert_not_called()

    def test_a_stage_the_policy_keeps_off_is_never_reported_as_a_fallback_or_a_failure(self):
        stack, _mocks = forbid_every_learned_adapter()
        with stack:
            _out, _level, parameters = enhance_bgr(PHOTOS["grayscale"](), "auto")
        for report in parameters["stage_reports"]:
            self.assertNotIn(report["status"], {"fallback", "failed"}, report)
        self.assertFalse(any("unavailable" in warning for warning in parameters["warnings"]), parameters["warnings"])

    def test_the_policy_is_recorded_with_what_it_overrode(self):
        stack, _mocks = forbid_every_learned_adapter()
        with stack:
            _out, _level, parameters = enhance_bgr(PHOTOS["sharp"](), "auto")
        recorded = parameters["render_policy"]
        self.assertFalse(recorded["learned_stages_allowed"])
        self.assertFalse(recorded["heavy_restoration_available"])
        self.assertEqual(recorded["configured_on_but_disabled_by_policy"], sorted(policy.LEARNED_STAGES.values()))
        flags = parameters["feature_flags"]
        self.assertFalse(any(flags[key] for key in (
            "gfpgan_enabled", "hd_restoration_enabled", "nafnet_deblur_enabled")))

    def test_ai_max_says_what_it_did_and_what_it_could_not(self):
        stack, _mocks = forbid_every_learned_adapter()
        with stack:
            _out, level, parameters = enhance_bgr(PHOTOS["blurred"](), "auto", style="ai_max")
        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertFalse(parameters["ai_max"]["heavy_restoration_available"])
        self.assertIn("learned_v1", parameters["ai_max"]["behaviour"])
        self.assertTrue(any("AI Max used the strongest local correction only" in w for w in parameters["warnings"]))

    def test_natural_mode_has_no_ai_max_block(self):
        stack, _mocks = forbid_every_learned_adapter()
        with stack:
            _out, _level, parameters = enhance_bgr(PHOTOS["sharp"](), "auto")
        self.assertNotIn("ai_max", parameters)

    def test_blur_is_reported_as_beyond_the_lightweight_pipeline_not_as_a_missing_model(self):
        stack, _mocks = forbid_every_learned_adapter()
        with stack:
            _out, _level, parameters = enhance_bgr(PHOTOS["blurred"](), "auto")
        message = " ".join(parameters["warnings"])
        self.assertIn("cannot be corrected by the lightweight pipeline", message)
        self.assertNotIn("requires an AI restoration model", message)

    def test_the_face_detector_and_identity_guard_still_run(self):
        """YuNet/SFace are not enhancement models: they stay available."""
        from django.conf import settings

        self.assertTrue(settings.IDENTITY_GUARD_ENABLED)
        _out, _level, parameters = enhance_bgr(PHOTOS["sharp"](), "auto")
        self.assertIn(parameters["identity_status"], {"verified", "inconclusive", "unavailable", "failed"})
        self.assertFalse(parameters["identity_verification"]["face_restoration_unverified"])
        self.assertIn("face_fidelity", parameters)

    @override_settings(NAFNET_MODEL_PATH="/nonexistent/nafnet.pth", REALESRGAN_MODEL_PATH="/nonexistent/esrgan.pth",
                       GFPGAN_MODEL_PATH="/nonexistent/gfpgan.pth")
    def test_system_health_does_not_demand_a_checkpoint_for_a_stage_that_cannot_run(self):
        from operations.services import check_models

        models = check_models()["models"] if "models" in check_models() else check_models()
        for name in ("nafnet", "realesrgan", "gfpgan"):
            self.assertEqual(models[name]["status"], "disabled", name)

    @override_settings(ENHANCEMENT_RENDER_POLICY="learned_v1", NAFNET_MODEL_PATH="/nonexistent/nafnet.pth")
    def test_under_learned_v1_a_missing_checkpoint_is_reported(self):
        from operations.services import check_models

        models = check_models()["models"] if "models" in check_models() else check_models()
        self.assertEqual(models["nafnet"]["status"], "degraded")


@override_settings(**ALL_LEARNED_ON, ENHANCEMENT_RENDER_POLICY="learned_v1")
class LearnedPolicyIsStillReachableTests(SimpleTestCase):
    """Reversibility: the adapters and their settings still work under learned_v1."""

    @override_settings(RETINEXFORMER_ENABLED=False)  # Retinexformer is preferred when on; test the Zero-DCE fallback here
    def test_learned_v1_runs_a_learned_stage_when_its_setting_is_on(self):
        source = PHOTOS["dark"]()
        with patch("enhancement.services.enhance_with_zero_dce",
                   return_value=(cv2.add(source, 40), {"provider": "zero_dce_test"})) as zero_dce:
            _out, _level, parameters = enhance_bgr(source, "auto")
        zero_dce.assert_called_once()
        self.assertEqual(parameters["render_policy"]["name"], "learned_v1")
        self.assertTrue(parameters["render_policy"]["heavy_restoration_available"])
        self.assertTrue(parameters["zero_dce"]["applied"])

    @override_settings(ZERO_DCE_ENABLED=False)
    def test_learned_v1_still_honours_each_setting(self):
        with patch("enhancement.services.enhance_with_zero_dce", side_effect=AssertionError("off")) as zero_dce:
            enhance_bgr(PHOTOS["dark"](), "auto")
        zero_dce.assert_not_called()

    def test_an_unknown_policy_name_is_refused(self):
        with override_settings(ENHANCEMENT_RENDER_POLICY="fast"):
            with self.assertRaises(ValueError):
                policy.active_policy()

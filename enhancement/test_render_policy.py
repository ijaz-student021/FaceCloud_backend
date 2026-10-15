"""The enhancement pipeline is OpenCV and Pillow only; the learned models are retired.

These tests keep it that way: the adapters and their settings are gone, a new
result never mentions a retired stage, the retirement is reported explicitly
(never as a "fallback" and never by substituting another algorithm), and the
things that were *not* retired (YuNet, SFace, the quality and face guards) still run.
"""
import importlib.util
from functools import lru_cache

import cv2
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import checks, policy
from .models import EnhancementLevel
from .services import enhance_bgr
from .test_blur_measure import blurred_photo, darkened, dead_leaves, resized

RETIRED_MODULES = ("zero_dce", "gfpgan", "hd_restoration", "purescale", "purevision", "colorization", "runtime")
RETIRED_ALGORITHM_PARTS = ("private_gfpgan", "realesrgan", "nafnet", "purescale", "purevision", "colorization", "zero_dce")
RETIRED_PARAMETER_KEYS = (
    "zero_dce", "restoration", "hd_restoration", "purevision", "purescale", "face_restoration", "colorization",
    "discarded_stage_reports",
)
RETAINED_STAGES = {
    "analyze_original", "exposure_enhancement", "output_guard_a", "measure_post_exposure_noise", "adaptive_denoise",
    "sharpen", "output_guard_b", "face_fidelity", "dynamic_style", "identity_guard", "validate_output",
}


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


class RetiredCodeIsGoneTests(SimpleTestCase):
    def test_the_adapters_are_not_importable(self):
        for name in RETIRED_MODULES:
            with self.subTest(name):
                self.assertIsNone(importlib.util.find_spec(f"enhancement.{name}"), f"enhancement.{name} came back")

    def test_the_retired_settings_no_longer_exist(self):
        present = [name for name in dir(settings) if name.isupper() and (
            name.startswith(policy.RETIRED_SETTING_PREFIXES) or name in policy.RETIRED_SETTING_NAMES)]
        self.assertEqual(present, [])

    def test_the_checkpoints_are_no_longer_expected_anywhere(self):
        from operations.services import check_models

        health = check_models()
        self.assertEqual(sorted(health["models"]), ["sface", "yunet"])
        self.assertEqual(health["retired"], sorted(policy.RETIRED_CAPABILITIES))


class LightweightPipelineTests(SimpleTestCase):
    @override_settings(IDENTITY_GUARD_ENABLED=True)
    def test_a_result_never_mentions_a_retired_stage_whatever_the_photo_level_or_style(self):
        for name, make in PHOTOS.items():
            for level in ("auto", "high"):
                for style in ("natural", "ai_max"):
                    with self.subTest(photo=name, level=level, style=style):
                        _out, _level, parameters = enhance_bgr(make(), level, style=style)
                        self.assertEqual(parameters["render_policy"]["name"], "lightweight_v1")
                        self.assertEqual(parameters["algorithm"].split("+")[0], "opencv_adaptive_restoration_router_v2")
                        for part in RETIRED_ALGORITHM_PARTS:
                            self.assertNotIn(part, parameters["algorithm"])
                        for key in RETIRED_PARAMETER_KEYS:
                            self.assertNotIn(key, parameters)
                        self.assertEqual(parameters["pipeline_version"], "staged_candidate_pipeline_v5")
                        self.assertEqual(parameters["resolution_contract"]["pixel_grid_source"], "original_pixel_grid")
                        stages = {report["stage"] for report in parameters["stage_reports"]}
                        self.assertLessEqual(stages, RETAINED_STAGES)
                        for report in parameters["stage_reports"]:
                            # A candidate the denoise guard rejects is a normal outcome; a failure or a
                            # "fallback" (something else standing in for a model) is not.
                            self.assertNotIn(report["status"], {"failed", "fallback"}, report)
                            if report["status"] == "rejected":
                                self.assertEqual(report["stage"], "adaptive_denoise", report)

    def test_the_retirement_is_recorded_on_every_result_and_is_not_a_fallback(self):
        _out, _level, parameters = enhance_bgr(PHOTOS["grayscale"](), "auto")
        recorded = parameters["render_policy"]
        self.assertFalse(recorded["heavy_restoration_available"])
        self.assertEqual({item["key"] for item in recorded["retired_capabilities"]}, set(policy.RETIRED_CAPABILITIES))
        self.assertTrue(all(item["status"] == "retired" and item["retired_on"] == "2026-10-15"
                            for item in recorded["retired_capabilities"]))
        self.assertEqual(recorded["documentation"], "docs/enhancement/retired-capabilities.md")
        self.assertFalse(any("unavailable" in warning for warning in parameters["warnings"]), parameters["warnings"])
        self.assertEqual(set(parameters["feature_flags"]),
                         {"adaptive_denoise_enabled", "identity_guard_enabled", "face_fidelity_enabled"})

    def test_ai_max_says_what_it_did_and_what_it_cannot_do(self):
        _out, level, parameters = enhance_bgr(PHOTOS["blurred"](), "auto", style="ai_max")
        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertFalse(parameters["ai_max"]["heavy_restoration_available"])
        self.assertIn("retired", parameters["ai_max"]["behaviour"])
        self.assertTrue(any("AI Max used the strongest local correction only" in w for w in parameters["warnings"]))

    def test_natural_mode_has_no_ai_max_block(self):
        _out, _level, parameters = enhance_bgr(PHOTOS["sharp"](), "auto")
        self.assertNotIn("ai_max", parameters)

    def test_blur_is_reported_as_beyond_the_lightweight_pipeline(self):
        _out, _level, parameters = enhance_bgr(PHOTOS["blurred"](), "auto")
        message = " ".join(parameters["warnings"])
        self.assertIn("cannot be corrected by the lightweight pipeline", message)
        self.assertNotIn("requires an AI restoration model", message)

    def test_a_dark_photo_is_not_lifted_by_automatic_mode(self):
        source = PHOTOS["dark"]()
        out, _level, parameters = enhance_bgr(source, "auto")
        self.assertTrue(parameters["no_correction_needed"] or abs(float(out.mean()) - float(source.mean())) < 6)
        self.assertEqual(parameters["plan"]["routing"][0]["decision"], "lift_withheld")

    @override_settings(IDENTITY_GUARD_ENABLED=True)
    def test_the_face_detector_identity_guard_and_face_check_still_run(self):
        """YuNet/SFace are not enhancement models: they stay available."""
        self.assertTrue(settings.IDENTITY_GUARD_ENABLED)
        _out, _level, parameters = enhance_bgr(PHOTOS["sharp"](), "auto")
        self.assertIn(parameters["identity_status"], {"verified", "inconclusive", "unavailable", "failed"})
        self.assertEqual(set(parameters["identity_verification"]), {"status", "verified", "reason", "notice"})
        self.assertIn("face_fidelity", parameters)
        self.assertIn("quality_guards", parameters)


class RetirementReportingTests(SimpleTestCase):
    def test_the_policy_names_every_retired_capability_and_never_a_replacement(self):
        names = {item["name"] for item in policy.retired_capabilities()}
        for expected in ("Zero-DCE", "EDSR", "NAFNet", "Real-ESRGAN", "GFPGAN", "PureVision", "PureScale", "colourisation"):
            self.assertTrue(any(expected.lower() in name.lower() for name in names), expected)
        self.assertIn("retired on 2026-10-15", policy.retirement_message("zero_dce"))
        self.assertIn(policy.RETIREMENT_DOCUMENT, policy.retirement_message("gfpgan_face_restoration"))

    def test_leftover_environment_settings_are_reported_not_ignored_in_silence(self):
        names = checks.retired_settings_in_environment({
            "ZERO_DCE_ENABLED": "True", "GFPGAN_QUEUE_NAME": "gfpgan", "ENHANCEMENT_RENDER_POLICY": "learned_v1",
            "PUREVISION_TILE_SIZE": "256", "DB_NAME": "x", "OPENAI_API_KEY": "x", "IDENTITY_MODEL_PATH": "y",
        })
        self.assertEqual(names, ["ENHANCEMENT_RENDER_POLICY", "GFPGAN_QUEUE_NAME", "PUREVISION_TILE_SIZE", "ZERO_DCE_ENABLED"])
        self.assertEqual(checks.retired_settings_in_environment({"DB_NAME": "x"}), [])

    def test_the_system_check_warns_when_one_is_set(self):
        with override_settings():
            import os
            from unittest.mock import patch

            with patch.dict(os.environ, {"COLORIZATION_ENABLED": "True"}):
                issues = checks.retired_enhancement_settings_check(None)
            self.assertEqual([issue.id for issue in issues], ["enhancement.W001"])
            self.assertIn("COLORIZATION_ENABLED", issues[0].msg)
            self.assertIn(policy.RETIREMENT_DOCUMENT, issues[0].hint)

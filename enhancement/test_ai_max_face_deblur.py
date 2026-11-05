"""AI Enhance (the `pro` preset) accepts a face deblur that raises the face's detail 1.10 times where the other styles need 1.25 (ADAPTIVE_AI_MAX_FACE_DEBLUR_MIN_DETAIL_GAIN).

Measured with the real NAFNet-GoPro on 210 LFW faces with a known clean photograph (docs/evaluation/2026-11-05-dark-face-identity.md): a deblur whose face-detail gain was 1.10 to 1.25 was closer to the
truth for 100 % of defocus-blurred faces and 90 % of low-resolution ones, and not for motion blur (17 %). Here the deblur is a stand-in (CI has no weights): these tests are about WHICH style gets the
lower limit and that every other gate is unchanged.
"""
from unittest.mock import patch

import cv2
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator
from .test_adaptive_polish import _no_identity

ENGINE_SETTINGS = {**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False}
SOURCE = cv2.GaussianBlur(base.portrait(), (0, 0), 2.0)       # a blurred face: the analyzer calls it `high`, face detail 2.8


def sharpen(amount):
    """A stand-in for the deblurring model: an unsharp mask of the given amount (its face-detail gain: 0.20 -> 1.07, 0.25 -> 1.21, 0.30 -> 1.32)."""
    def run(image, engine, variant):
        return cv2.addWeighted(image, 1 + amount, cv2.GaussianBlur(image, (0, 0), 1.5), -amount, 0), [{"model": engine, "device": "stand-in"}]
    return run


def face_gain(amount):
    analyzer = base.analyzer_with()
    return analyzer.analyze(sharpen(amount)(SOURCE, "x", None)[0]).face_blur["detail"] / max(analyzer.analyze(SOURCE).face_blur["detail"], 1.0)


def deblur_stage(style="natural", level="auto", amount=0.25, **settings_):
    service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(), config=EngineConfig(), runners={ops.DEBLUR: sharpen(amount)})
    with override_settings(**{**ENGINE_SETTINGS, **settings_}), patch("enhancement.adaptive.service.IdentityChecker", _no_identity):
        _output, _level, parameters = service.enhance(SOURCE, requested_level=level, style=style)
    return next(stage for stage in parameters["adaptive"]["stages"] if stage["operation"] == ops.DEBLUR)


class ProfileTests(SimpleTestCase):
    def test_the_default_and_the_setting(self):
        self.assertEqual(settings.ADAPTIVE_AI_MAX_FACE_DEBLUR_MIN_DETAIL_GAIN, 1.10)
        self.assertEqual(settings.ADAPTIVE_FACE_DEBLUR_MIN_DETAIL_GAIN, 1.25)
        with override_settings(ADAPTIVE_AI_MAX_FACE_DEBLUR_MIN_DETAIL_GAIN=1.25):
            self.assertEqual(profile_for("ai_max").face_deblur_min_detail_gain, 1.25)

    def test_only_ai_enhance_asks_for_less(self):
        for level in ("auto", "low", "medium", "high"):
            self.assertEqual(profile_for("ai_max", level).face_deblur_min_detail_gain, 1.10, level)
            self.assertIsNone(profile_for("natural", level).face_deblur_min_detail_gain, level)
            self.assertIsNone(profile_for("dynamic", level).face_deblur_min_detail_gain, level)

    def test_it_is_in_the_profile_the_result_records(self):
        self.assertEqual(profile_for("ai_max").describe()["face_deblur_min_detail_gain"], 1.10)
        self.assertIsNone(profile_for("natural").describe()["face_deblur_min_detail_gain"])


class EngineTests(SimpleTestCase):
    def test_the_premise_the_stand_in_deblur_gains_between_the_two_limits(self):
        self.assertGreater(face_gain(0.25), 1.10)
        self.assertLess(face_gain(0.25), 1.25)
        self.assertLess(face_gain(0.20), 1.10)
        self.assertGreater(face_gain(0.30), 1.25)

    def test_ai_enhance_accepts_it_and_the_natural_style_does_not(self):
        natural = deblur_stage("natural")
        self.assertEqual((natural["status"], natural["rejection_reasons"]), ("rejected", ["face_not_clearer_or_over_sharpened"]))
        self.assertEqual(deblur_stage("ai_max")["status"], "accepted")

    def test_pro_accepts_it_and_standard_stays_conservative(self):
        self.assertEqual(deblur_stage("ai_max", "high")["status"], "accepted")                       # the `pro` preset: high + ai_max
        self.assertEqual(deblur_stage("natural", "high")["status"], "rejected")                      # the `standard` preset: high + natural
        self.assertEqual(deblur_stage("dynamic", "medium")["status"], "rejected")

    def test_a_deblur_that_does_not_clearly_help_is_still_refused_for_ai_enhance(self):
        stage = deblur_stage("ai_max", amount=0.20)                                                  # gain 1.07: under even the lower limit
        self.assertEqual((stage["status"], stage["rejection_reasons"]), ("rejected", ["face_not_clearer_or_over_sharpened"]))

    def test_a_deblur_that_helps_clearly_is_accepted_by_both(self):
        for style in ("natural", "ai_max"):
            self.assertEqual(deblur_stage(style, amount=0.30)["status"], "accepted", style)

    def test_the_other_gates_still_apply_to_ai_enhance(self):
        stage = deblur_stage("ai_max", amount=4.0)                                                   # over-sharpened: edge gain and consistency
        self.assertEqual(stage["status"], "rejected")
        self.assertTrue(stage["rejection_reasons"])

    def test_the_setting_is_the_lower_limit_and_1_25_gives_ai_enhance_the_common_one(self):
        self.assertEqual(deblur_stage("ai_max", ADAPTIVE_AI_MAX_FACE_DEBLUR_MIN_DETAIL_GAIN=1.25)["status"], "rejected")

    def test_a_lower_limit_the_operator_set_is_never_raised(self):
        """The profile can lower the limit, never raise it: with the operator's own limit at 1.0 a style that asks for 1.10 still gets 1.0."""
        recorded = []
        real = FinalImageQualityValidator

        def recording(*args, **kwargs):
            recorded.append(kwargs["limits"].face_deblur_min_detail_gain)
            return real(*args, **kwargs)

        with patch("enhancement.adaptive.service.FinalImageQualityValidator", recording):
            deblur_stage("ai_max", ADAPTIVE_FACE_DEBLUR_MIN_DETAIL_GAIN=1.0)
            deblur_stage("ai_max")
            deblur_stage("natural")
        self.assertEqual(recorded, [1.0, 1.10, 1.25])

"""QUALITY_REJECT_SEVERE_BLUR: a photograph whose blur measures "severe" is refused and never enhanced; a mild blur still goes to the restoration chain.

Off by default (the product's job is to deblur a soft photograph, see test_never_reject.py). Found when a soft, washed-out portrait was enhanced and the owner wanted such pictures refused:
the live data holds no severe case, the owner's mild 14.jpg (edge sharpness 0.302) stays untouched.
"""
import cv2
from django.test import SimpleTestCase, TestCase, override_settings

from enhancement.eligibility import rejection_of
from enhancement.models import ImageEnhancementResult
from identities.verdict import verdict_allows_grouping
from quality_control.effective import effective_decision
from quality_control.models import ImageQualityDecision
from quality_control.services import calculate_scores, make_decision
from quality_control.test_dark_photos import MEDIA, EndToEndTests, synthetic_photo
from uploads.display_status import image_display_status

CLEAN = {"width": 640, "height": 480, "mean_luminance": 120.0, "contrast_stddev": 55.0, "laplacian_sharpness": 150.0, "estimated_noise": 1.0, "highlight_clipping_percent": 0.0,
         "shadow_clipping_percent": 0.0, "defects": [], "blur": {"verdict": "sharp", "edge_sharpness": 1.2}}
FACE = type("Face", (), {"decision": "accepted", "face_count": 1, "rejection_reason": ""})()


def blurred(sigma):
    return cv2.GaussianBlur(synthetic_photo(mean=120.0, spread=50.0), (0, 0), sigma)


def decide(verdict, face=True, coarse=None):
    found = CLEAN | {"blur": {"verdict": verdict}, "blur_coarse": coarse}
    return make_decision(found, calculate_scores(found), FACE if face else None)


class RuleTests(SimpleTestCase):
    @override_settings(QUALITY_REJECT_SEVERE_BLUR=True)
    def test_a_severe_blur_is_refused_with_or_without_a_face(self):
        for face in (True, False):
            decision, reasons, message = decide("severe", face)
            self.assertEqual((decision, reasons), ("rejected", ["severe_blur"]))
            self.assertIn("too blurry", message)

    @override_settings(QUALITY_REJECT_SEVERE_BLUR=True)
    def test_a_mild_or_unmeasured_blur_is_not_refused(self):
        for verdict in ("mild", "sharp", "uncertain", None):
            self.assertNotEqual(decide(verdict)[0], "rejected", verdict)

    @override_settings(QUALITY_REJECT_SEVERE_BLUR=True)
    def test_a_grainy_defocused_photograph_is_caught_at_the_coarse_scale(self):
        # Measured on a real one: "sharp" (0.504) at 1024 px, 0.279 at 512 px; the next-lowest of 136 live photographs was 0.336.
        decision, reasons, _ = decide("sharp", coarse={"edge_sharpness": 0.279, "verdict": "mild", "measured_at_long_edge": 512})
        self.assertEqual((decision, reasons), ("rejected", ["severe_blur"]))
        for coarse in ({"edge_sharpness": 0.336, "verdict": "mild"}, {"edge_sharpness": 0.2, "verdict": "uncertain"}, None):
            self.assertNotEqual(decide("sharp", coarse=coarse)[0], "rejected", coarse)

    @override_settings(QUALITY_REJECT_SEVERE_BLUR=True, QUALITY_REJECT_BLUR_COARSE_EDGE=0.40)
    def test_the_coarse_line_is_a_setting(self):
        self.assertEqual(decide("sharp", coarse={"edge_sharpness": 0.336, "verdict": "mild"})[0], "rejected")

    def test_the_coarse_measure_only_exists_for_a_photograph_larger_than_the_normal_scale(self):
        from enhancement.services import coarse_blur

        photo = synthetic_photo(mean=120.0, spread=50.0)
        self.assertIsNone(coarse_blur(cv2.cvtColor(photo, cv2.COLOR_RGB2GRAY)))                   # 480 px: measured at its own size either way
        big = cv2.resize(photo, None, fx=3, fy=3, interpolation=cv2.INTER_LINEAR)
        found = coarse_blur(cv2.cvtColor(big, cv2.COLOR_RGB2GRAY))
        self.assertEqual(found["measured_at_long_edge"], 512)

    def test_off_by_default_a_severe_blur_is_not_refused(self):
        self.assertFalse(__import__("django.conf", fromlist=["settings"]).settings.QUALITY_REJECT_SEVERE_BLUR)
        self.assertNotEqual(decide("severe")[0], "rejected")


@override_settings(QUALITY_REJECT_SEVERE_BLUR=True, MEDIA_ROOT=MEDIA, IMAGE_ENCRYPTION_ENABLED=False)
class EndToEndBlurTests(TestCase):
    setUp = EndToEndTests.setUp
    stored = EndToEndTests.stored
    enhance = EndToEndTests.enhance

    def test_a_very_blurry_photograph_is_rejected_and_not_enhanced_or_grouped(self):
        image = self.stored(blurred(3))
        decision = ImageQualityDecision.objects.get(image=image)
        self.assertEqual((decision.decision, decision.rejection_reasons), ("rejected", ["severe_blur"]))
        self.assertEqual(image.status, "rejected")
        self.assertIn("too blurry", image.rejection_reason)
        self.assertEqual(image_display_status(image), "rejected")
        self.assertEqual(effective_decision(image), "rejected")                    # QUALITY_CHECKS_REJECT is off: this one still stands
        self.assertIsNotNone(rejection_of(image))
        self.assertFalse(verdict_allows_grouping(image)[0])
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())      # no automatic enhancement either
        self.assertIn(self.enhance(image).status_code, (400, 404))                           # refused: a rejected upload is not even offered to the enhancement request
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())

    def test_a_mildly_soft_photograph_is_still_enhanced(self):
        image = self.stored(blurred(1.5))
        decision = ImageQualityDecision.objects.get(image=image)
        self.assertNotEqual(decision.decision, "rejected", decision.rejection_reasons)
        self.assertEqual(image.status, "uploaded")
        self.assertEqual(self.enhance(image).status_code, 202)

    @override_settings(QUALITY_REJECT_SEVERE_BLUR=False)
    def test_with_the_flag_off_a_very_blurry_photograph_is_enhanced_as_before(self):
        image = self.stored(blurred(3))
        self.assertEqual(image.status, "uploaded")
        self.assertNotEqual(ImageQualityDecision.objects.get(image=image).decision, "rejected")
        self.assertEqual(self.enhance(image).status_code, 202)

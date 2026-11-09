"""A quality check never refuses a photograph (QUALITY_CHECKS_REJECT, off): the product's job is to enhance it.

Found on a real portrait (3456 x 5184, mean brightness 29) refused as "Too blurry" with "0 faces": a dark photograph has weak gradients and reads as blurry, the face detector missed its
large face at full size, and the engine, once allowed to run, brightened it to a natural 82.5. See docs/evaluation/2026-11-03-enhance-every-photograph.md.
"""
from types import SimpleNamespace

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from enhancement.auto import NEVER, NOW_PERSONAL, WAIT_FOR_MODERATOR, verdict_allows_enhancement
from enhancement.eligibility import not_rejected, rejection_of
from enhancement.services import analyze_image_quality
from quality_control.test_effective_decision import EffectiveDecisionBase
from quality_control.effective import effective_decision, effective_q, review_required
from quality_control.services import calculate_scores, make_decision
from uploads.display_status import image_display_status
from uploads.models import ImageUpload

NO_FACE = SimpleNamespace(face_count=0, faces=[], decision="no_face", rejection_reason="")
FACE = SimpleNamespace(face_count=1, faces=[], decision="accepted", rejection_reason="")


def metrics(**changes):
    base = {"width": 3000, "height": 2000, "mean_luminance": 120.0, "contrast_stddev": 55.0, "laplacian_sharpness": 150.0, "estimated_noise": 1.0, "blur": {}, "luminance_percentiles": {},
            "highlight_clipping_percent": 0.0, "shadow_clipping_percent": 0.0, "dark_pixel_percent": 10.0, "bright_pixel_percent": 1.0, "defects": [], "needs_ai_restoration": False}
    return base | changes


FORMER_REJECTS = {
    "severe_blur": metrics(laplacian_sharpness=5.0),
    "severe_underexposure": metrics(mean_luminance=2.0, contrast_stddev=10.0),
    "severe_overexposure": metrics(mean_luminance=250.0),
    "contrast_too_low": metrics(contrast_stddev=1.0),
    "resolution_too_low": metrics(width=120, height=100),
}


class TheDecisionNeverRefusesTests(SimpleTestCase):
    def test_every_reason_that_used_to_refuse_a_photograph_now_sends_it_on_with_the_reason_kept(self):
        for reason, found in FORMER_REJECTS.items():
            decision, reasons, message = make_decision(found, calculate_scores(found), NO_FACE)
            self.assertEqual(decision, "needs_review", reason)
            self.assertIn(reason, reasons)
            self.assertIn("still enhanced", message)

    @override_settings(QUALITY_CHECKS_REJECT=True)
    def test_the_old_behaviour_is_one_setting_away(self):
        for reason, found in FORMER_REJECTS.items():
            decision, reasons, _message = make_decision(found, calculate_scores(found), NO_FACE)
            self.assertEqual(decision, "rejected", reason)
            self.assertIn(reason, reasons)

    def test_a_good_photograph_is_still_accepted(self):
        good = metrics()
        self.assertEqual(make_decision(good, calculate_scores(good), FACE)[0], "accepted")

    def test_a_dark_photograph_is_not_mistaken_for_a_blurry_one(self):
        """The same natural-looking picture at 11 percent of the light: measured as before its sharpness was 17.5 (under the refusal line of 20: "Too blurry"); brightened first it is 110."""
        from django.conf import settings

        from enhancement.test_blur_measure import dead_leaves, resized

        bright = resized(dead_leaves(), 900)
        dark = (bright.astype(np.float32) * 0.11).astype(np.uint8)
        old_measure = float(cv2.Laplacian(cv2.cvtColor(dark, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        measured = analyze_image_quality(dark)
        self.assertLess(measured["mean_luminance"], 20.0)
        self.assertLess(old_measure, settings.QUALITY_REJECT_SHARPNESS)
        self.assertGreater(measured["laplacian_sharpness"], settings.QUALITY_REVIEW_SHARPNESS)
        self.assertGreater(measured["laplacian_sharpness"], 4 * old_measure)

    def test_a_photograph_that_is_not_dark_is_measured_as_it_always_was(self):
        image = np.random.default_rng(5).integers(80, 200, (300, 400, 3), dtype=np.uint8)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        self.assertEqual(analyze_image_quality(image)["laplacian_sharpness"], round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2))


class OldRowsFollowTheSettingTests(EffectiveDecisionBase):
    """A quality decision stored as "rejected" before this change does not block anything while the setting is off."""

    def photos(self):
        return (self.photo(self.batch("personal"), "rejected", ["severe_blur"]), self.photo(self.batch("team"), "rejected", ["severe_blur"]))

    def test_a_stored_rejection_means_accepted_with_tips_for_a_person_and_review_for_a_team(self):
        personal, team = self.photos()
        self.assertEqual(effective_decision(personal), "accepted")
        self.assertEqual(effective_decision(team), "needs_review")
        self.assertTrue(review_required(team))
        self.assertNotEqual(image_display_status(personal), "rejected")

    def test_the_database_condition_agrees(self):
        personal, team = self.photos()
        accepted = set(ImageUpload.objects.filter(effective_q("accepted")).values_list("pk", flat=True))
        self.assertIn(personal.pk, accepted)
        self.assertNotIn(team.pk, accepted)
        self.assertEqual(set(ImageUpload.objects.filter(effective_q("needs_review")).values_list("pk", flat=True)), {team.pk})
        self.assertFalse(ImageUpload.objects.filter(effective_q("rejected")).exists())

    def test_such_a_photograph_may_be_enhanced_by_request_and_automatically(self):
        personal, team = self.photos()
        self.assertIsNone(rejection_of(personal))
        self.assertEqual(set(not_rejected(ImageUpload.objects.all()).values_list("pk", flat=True)), {personal.pk, team.pk})
        self.assertEqual(verdict_allows_enhancement(personal), (True, NOW_PERSONAL))
        self.assertEqual(verdict_allows_enhancement(team), (False, WAIT_FOR_MODERATOR))

    @override_settings(QUALITY_CHECKS_REJECT=True)
    def test_with_the_setting_on_a_stored_rejection_blocks_as_it_always_did(self):
        personal, _team = self.photos()
        self.assertEqual(effective_decision(personal), "rejected")
        self.assertEqual(image_display_status(personal), "rejected")
        self.assertIsNotNone(rejection_of(personal))
        self.assertEqual(verdict_allows_enhancement(personal), (False, NEVER))

    def test_an_unreadable_upload_is_still_refused(self):
        personal, _team = self.photos()
        ImageUpload.objects.filter(pk=personal.pk).update(status="rejected")
        broken = ImageUpload.objects.get(pk=personal.pk)
        self.assertIsNotNone(rejection_of(broken))
        self.assertEqual(verdict_allows_enhancement(broken), (False, NEVER))

"""A dark face is embedded brightened (the embedding only), not refused at a mean of 45: IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD, IDENTITY_GUARD_DARK_FACE_GAIN, IDENTITY_GUARD_MIN_FACE_LUMINANCE.

The reason: a 47 px face in a dim photograph had a box mean of 41.4, so the guard refused to build an identity reference ("The source face is too dark for a reliable identity comparison") and
no generative face restoration could run. Measured with the real SFace on 160 public LFW faces darkened to a known mean (docs/evaluation/2026-11-05-dark-face-identity.md): brightening the crop
raises how well the same face matches itself and does not move a look-alike impostor. The provider here is a stand-in (CI has no weights), so these tests are about WHERE and WHEN the crop is brightened.
"""
from unittest.mock import patch

import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from .adaptive.service import IdentityChecker
from .identity_guard import _brightened_for_embedding, _embed_faces, build_identity_reference, compare_faces
from .test_identity_guard import FakeDetector, FakeProvider

BOX = (20, 20, 80, 80)


class RecordingProvider(FakeProvider):
    """The stand-in of the other identity tests (its embedding angle follows the crop's brightness) that also keeps the crops it was asked to embed."""

    def __init__(self):
        self.embedded = []

    def prepare_face(self, image, box, landmarks=None):
        x, y, width, height = box["x"], box["y"], box["width"], box["height"]
        return image[y:y + height, x:x + width].copy()

    def embed(self, prepared):
        self.embedded.append(prepared)
        return super().embed(prepared)


def photo(level, size=200):
    return np.full((size, size, 3), level, dtype=np.uint8)


def patched(detector, provider):
    return (patch("enhancement.identity_guard._load_detector", return_value=detector), patch("enhancement.identity_guard._load_provider", return_value=provider))


class SettingsTests(SimpleTestCase):
    def test_the_defaults(self):
        self.assertEqual(settings.IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD, 40.0)
        self.assertEqual(settings.IDENTITY_GUARD_DARK_FACE_GAIN, 2.5)
        self.assertEqual(settings.IDENTITY_GUARD_MIN_FACE_LUMINANCE, 15.0)


class BrightenedForEmbeddingTests(SimpleTestCase):
    def test_a_dark_crop_is_brightened_by_the_gain_and_the_original_is_not_touched(self):
        crop = photo(30)
        out = _brightened_for_embedding(crop)
        self.assertIsNot(out, crop)
        self.assertEqual(int(out.mean()), 75)                          # 30 x 2.5
        self.assertTrue((crop == 30).all(), "the crop that was given must stay as it was")
        self.assertEqual(out.dtype, np.uint8)

    def test_a_crop_at_or_above_the_threshold_is_returned_as_it_is(self):
        for level in (40, 41, 90, 200):
            crop = photo(level)
            self.assertIs(_brightened_for_embedding(crop), crop, level)

    def test_a_crop_just_under_the_threshold_is_brightened(self):
        self.assertEqual(int(_brightened_for_embedding(photo(39)).mean()), 98)         # 39 x 2.5 = 97.5, rounded

    @override_settings(IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD=0)
    def test_with_the_threshold_at_zero_nothing_is_brightened(self):
        crop = photo(10)
        self.assertIs(_brightened_for_embedding(crop), crop)

    @override_settings(IDENTITY_GUARD_DARK_FACE_GAIN=3.0, IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD=60)
    def test_the_gain_and_the_threshold_are_the_settings(self):
        self.assertEqual(int(_brightened_for_embedding(photo(20)).mean()), 60)
        crop = photo(60)
        self.assertIs(_brightened_for_embedding(crop), crop)

    def test_a_bright_spot_saturates_instead_of_wrapping(self):
        crop = photo(20)
        crop[:5, :5] = 200
        out = _brightened_for_embedding(crop)
        self.assertEqual(int(out[0, 0, 0]), 255)
        self.assertEqual(int(out[10, 10, 0]), 50)

    def test_anything_that_is_not_a_three_channel_8_bit_crop_is_left_alone(self):
        for odd in ("aligned", np.zeros((10, 10), np.uint8), np.zeros((10, 10, 3), np.float32), np.zeros((10, 10, 4), np.uint8), None):
            self.assertIs(_brightened_for_embedding(odd), odd)


class EmbeddingTests(SimpleTestCase):
    def test_only_the_dark_face_is_brightened_and_the_photograph_is_not_changed(self):
        image = photo(120)
        image[20:100, 20:100] = 30                                          # one dark face ...
        image[120:190, 120:190] = 130                                       # ... and one normal one
        provider = RecordingProvider()
        records = _embed_faces(image, [((20, 20, 80, 80), []), ((120, 120, 70, 70), [])], provider)
        self.assertEqual(len(records), 2)
        self.assertEqual([int(crop.mean()) for crop in provider.embedded], [75, 130])
        self.assertTrue((image[20:100, 20:100] == 30).all())

    def test_the_reference_and_the_candidate_are_treated_by_the_same_rule(self):
        """A dark upload and its lifted version are one person: after the dark crop is brightened they are closer than they were."""
        source, lifted = photo(30), photo(60)
        detector = FakeDetector([BOX])
        similarities = {}
        for label, threshold in (("brightened", 40), ("as is", 0)):
            provider = RecordingProvider()
            with override_settings(IDENTITY_GUARD_DARK_FACE_BRIGHTNESS_THRESHOLD=threshold, IDENTITY_GUARD_MIN_FACE_LUMINANCE=10.0), patched(detector, provider)[0], patched(detector, provider)[1]:
                reference, _reason = build_identity_reference(source)
                similarities[label] = compare_faces(reference, lifted)[0]["similarity"]
        self.assertGreater(similarities["brightened"], similarities["as is"])
        self.assertGreater(similarities["brightened"], 0.97)


class ReferenceTests(SimpleTestCase):
    def build(self, level, **settings_):
        provider = RecordingProvider()
        patches = patched(FakeDetector([BOX]), provider)
        with override_settings(**settings_), patches[0], patches[1]:
            reference, reason = build_identity_reference(photo(level))
        return reference, reason, provider

    def test_the_face_that_started_this_is_no_longer_refused(self):
        """A box mean of 41 was under the old floor of 45: no reference, so no face restoration."""
        reference, reason, provider = self.build(41)
        self.assertIsNotNone(reference, reason)
        self.assertEqual(reason, "")
        self.assertEqual(len(provider.embedded), 1)
        self.assertEqual(int(provider.embedded[0].mean()), 41)             # 41 is above the brightening threshold: embedded as it is

    def test_a_darker_face_is_embedded_brightened(self):
        reference, _reason, provider = self.build(25)
        self.assertIsNotNone(reference)
        self.assertEqual(int(provider.embedded[0].mean()), 62)

    def test_a_face_below_the_floor_is_still_too_dark_and_nothing_is_loaded(self):
        provider = RecordingProvider()
        with patch("enhancement.identity_guard._load_detector", return_value=FakeDetector([BOX])), patch("enhancement.identity_guard._load_provider") as load:
            reference, reason = build_identity_reference(photo(10))
        self.assertIsNone(reference)
        self.assertEqual(reason, "The source face is too dark for a reliable identity comparison.")
        load.assert_not_called()
        self.assertEqual(provider.embedded, [])

    def test_the_old_floor_is_a_setting_away(self):
        reference, reason, _ = self.build(41, IDENTITY_GUARD_MIN_FACE_LUMINANCE=45.0)
        self.assertIsNone(reference)
        self.assertIn("too dark", reason)


class AdaptiveEngineIdentityTests(SimpleTestCase):
    """What the engine asks: is identity available for this upload? (The face restoration step is skipped when it is not.)"""

    def checker(self, level):
        provider = RecordingProvider()
        patches = patched(FakeDetector([BOX]), provider)
        with patches[0], patches[1]:
            return IdentityChecker(photo(level), True)

    def test_a_dim_face_now_has_an_identity_reference(self):
        checker = self.checker(41)
        self.assertTrue(checker.available)
        self.assertEqual(checker.reason, "")

    def test_a_face_that_is_too_dark_still_has_none_and_says_why(self):
        checker = self.checker(8)
        self.assertFalse(checker.available)
        self.assertIn("too dark", checker.reason)       # the engine's lifted-photograph fallback keys on this wording

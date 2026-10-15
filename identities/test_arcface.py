"""ArcFace as an alternative identity provider, per-provider thresholds, and the RetinaFace detector provider.

SFace stays the default and keeps the settings-based thresholds; ArcFace has its own because its cosine scale differs.
Tests that need a real model skip when the file or the runtime is not on this machine.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from validation import services as validation_services

from .providers import ArcFaceOnnxProvider, DisabledIdentityProvider, SFaceOnnxProvider, get_identity_provider

LANDMARKS = [(100.0, 120.0), (160.0, 120.0), (130.0, 160.0), (105.0, 190.0), (155.0, 190.0)]


def model_present():
    return Path(settings.ARCFACE_MODEL_PATH).is_file() and Path(settings.ARCFACE_LICENSE_RECORD_PATH).is_file()


class ProviderSelectionTests(SimpleTestCase):
    @override_settings(IDENTITY_PROVIDER="arcface_w600k_r50_v1")
    def test_the_arcface_provider_is_selected_by_name(self):
        provider = get_identity_provider()
        self.assertIsInstance(provider, ArcFaceOnnxProvider)
        self.assertEqual((provider.name, provider.dimension), ("arcface_w600k_r50_v1", 512))

    @override_settings(IDENTITY_PROVIDER="disabled")
    def test_the_default_remains_disabled_until_a_provider_is_chosen(self):
        self.assertIsInstance(get_identity_provider(), DisabledIdentityProvider)

    def test_sface_keeps_the_settings_based_thresholds_and_arcface_has_its_own(self):
        sface = SFaceOnnxProvider().thresholds()
        self.assertEqual(sface, {
            "guard": float(settings.IDENTITY_GUARD_MIN_SIMILARITY), "match": float(settings.IDENTITY_MATCH_THRESHOLD),
            "new_group": float(settings.IDENTITY_NEW_GROUP_THRESHOLD), "margin": float(settings.IDENTITY_AMBIGUITY_MARGIN),
        })
        arcface = ArcFaceOnnxProvider().thresholds()
        self.assertEqual(arcface["guard"], settings.ARCFACE_GUARD_MIN_SIMILARITY)
        self.assertLess(arcface["new_group"], arcface["match"])  # the same ordering rule the system check enforces
        self.assertNotEqual(arcface["guard"], sface["guard"])


class ArcFaceProviderTests(SimpleTestCase):
    @override_settings(ARCFACE_MODEL_PATH="/nonexistent/arcface.onnx")
    def test_a_missing_model_is_refused(self):
        with self.assertRaises(ValidationError):
            ArcFaceOnnxProvider().healthcheck()

    @override_settings(ARCFACE_MODEL_SHA256="0" * 64)
    def test_a_model_that_does_not_match_its_pinned_checksum_is_refused(self):
        if not Path(settings.ARCFACE_MODEL_PATH).is_file():
            self.skipTest("The ArcFace model is not installed here.")
        with self.assertRaises(ValidationError):
            ArcFaceOnnxProvider().healthcheck()

    def test_alignment_needs_five_landmarks(self):
        with self.assertRaises(ValidationError):
            ArcFaceOnnxProvider().prepare_face(np.zeros((200, 200, 3), np.uint8), {"x": 0, "y": 0, "width": 10, "height": 10}, None)

    def test_the_real_model_embeds_to_512_dimensions_and_recognises_the_same_face(self):
        if not model_present():
            self.skipTest("The ArcFace model is not installed here.")
        rng = np.random.default_rng(3)
        picture = np.clip(rng.normal(128, 40, (240, 240, 3)), 0, 255).astype(np.uint8)
        provider = ArcFaceOnnxProvider()
        box = {"x": 80, "y": 90, "width": 100, "height": 120}
        first = provider.embed(provider.prepare_face(picture, box, LANDMARKS))
        again = provider.embed(provider.prepare_face(picture.copy(), box, LANDMARKS))
        other = provider.embed(provider.prepare_face(cv2.flip(picture, 0), box, LANDMARKS))
        self.assertEqual(first.shape, (512,))
        self.assertAlmostEqual(float(np.linalg.norm(first)), 1.0, places=4)
        self.assertGreater(provider.similarity(first, again), 0.999)
        self.assertLess(provider.similarity(first, other), 0.99)


class GuardUsesTheProvidersThresholdTests(SimpleTestCase):
    def test_the_identity_guard_reports_the_threshold_of_the_provider_in_use(self):
        from enhancement import identity_guard

        class Provider(SFaceOnnxProvider):
            guard_min_similarity = 0.55
            embedding_model_label = "stand_in_similarity"

        class Reference:
            records, detector, provider, height, width = [], None, Provider(), 10, 10

        with patch.object(identity_guard, "_worst_face_similarity", return_value=0.9):
            _image, metadata = identity_guard.apply_identity_guard(
                np.zeros((10, 10, 3), np.uint8), np.zeros((10, 10, 3), np.uint8), reference=Reference(),
            )
        self.assertEqual((metadata["threshold"], metadata["model"]), (0.55, "stand_in_similarity"))


class RetinaFaceProviderTests(SimpleTestCase):
    @override_settings(FACE_DETECTOR_PROVIDER="retinaface_v1")
    def test_the_detector_is_selected_by_name(self):
        with patch.object(validation_services, "RetinaFaceDetector") as detector:
            validation_services.get_face_detector()
        detector.assert_called_once()

    @override_settings(GFPGAN_DETECTION_MODEL_PATH="/nonexistent/retinaface.pth")
    def test_a_missing_weight_file_is_refused_before_anything_loads(self):
        with self.assertRaises(ValidationError):
            validation_services.RetinaFaceDetector()

    def test_the_real_detector_finds_a_face_and_five_landmarks(self):
        if importlib.util.find_spec("facexlib") is None or not Path(settings.GFPGAN_DETECTION_MODEL_PATH).is_file():
            self.skipTest("facexlib or the RetinaFace weights are not available in this environment.")
        photos = sorted(Path("/Users/Ali/Desktop/images").glob("*.jpeg"))
        if not photos:
            self.skipTest("No sample portrait is available.")
        detection = validation_services.RetinaFaceDetector().detect(cv2.imread(str(photos[0])))
        self.assertGreaterEqual(len(detection.boxes), 1)
        self.assertEqual(len(detection.landmarks[0]), 5)
        self.assertEqual(detection.detector, "retinaface_resnet50")

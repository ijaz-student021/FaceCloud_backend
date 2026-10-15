from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from .identity_guard import (
    IdentityGuardUnavailable,
    _fit_to_edge,
    _iou,
    _load_provider,
    apply_identity_guard,
)
from .services import enhance_bgr

LANDMARKS = [(10, 10), (20, 10), (15, 15), (10, 20), (20, 20)]


class FakeDetector:
    """Returns fixed face boxes; ``sequence`` overrides them call by call."""

    def __init__(self, boxes, sequence=None):
        self.boxes = boxes
        self.sequence = list(sequence) if sequence is not None else None

    def detect(self, image):
        boxes = self.sequence.pop(0) if self.sequence else self.boxes
        return SimpleNamespace(boxes=boxes, landmarks=[LANDMARKS] * len(boxes))


class FakeProvider:
    """Embedding angle follows face brightness, so brightness drift lowers similarity."""

    dimension = 2

    def healthcheck(self):
        return True

    def prepare_face(self, image, box, landmarks=None):
        x, y, width, height = box["x"], box["y"], box["width"], box["height"]
        return image[y:y + height, x:x + width]

    def embed(self, prepared):
        angle = float(prepared.mean()) / 255.0 * np.pi
        return np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)

    def similarity(self, first, second):
        return float(np.dot(first, second))


def _patched(detector):
    return (
        patch("enhancement.identity_guard._load_detector", return_value=detector),
        patch("enhancement.identity_guard._load_provider", return_value=FakeProvider()),
    )


@override_settings(
    IDENTITY_GUARD_MIN_SIMILARITY=0.60,
    IDENTITY_GUARD_STRENGTHS=(1.0, 0.85, 0.7, 0.55, 0.4),
    IDENTITY_GUARD_MIN_FACE_EDGE=40,
    IDENTITY_GUARD_MIN_FACE_LUMINANCE=45.0,
    IDENTITY_GUARD_MAX_FACES=5,
    IDENTITY_GUARD_MAX_EDGE=1024,
)
class IdentityGuardAdapterTests(SimpleTestCase):
    def test_helpers(self):
        self.assertEqual(_fit_to_edge(np.zeros((1000, 2000, 3), dtype=np.uint8), 1024).shape[:2], (512, 1024))
        self.assertEqual(_fit_to_edge(np.zeros((100, 200, 3), dtype=np.uint8), 1024).shape[:2], (100, 200))
        self.assertAlmostEqual(_iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)
        self.assertEqual(_iou((0, 0, 10, 10), (50, 50, 10, 10)), 0.0)

    def test_no_face_in_source_skips_without_loading_the_identity_model(self):
        source = np.full((200, 200, 3), 50, dtype=np.uint8)
        candidate = np.full((200, 200, 3), 200, dtype=np.uint8)
        with (
            patch("enhancement.identity_guard._load_detector", return_value=FakeDetector([])),
            patch("enhancement.identity_guard._load_provider") as load_provider,
        ):
            output, metadata = apply_identity_guard(source, candidate)
        self.assertIs(output, candidate)
        self.assertEqual(metadata["status"], "skipped")
        self.assertIn("No face", metadata["fallback_reason"])
        load_provider.assert_not_called()

    def test_tiny_faces_are_not_judged(self):
        source = np.full((200, 200, 3), 50, dtype=np.uint8)
        with (
            patch("enhancement.identity_guard._load_detector", return_value=FakeDetector([(10, 10, 20, 20)])),
            patch("enhancement.identity_guard._load_provider") as load_provider,
        ):
            _output, metadata = apply_identity_guard(source, source.copy())
        self.assertEqual(metadata["status"], "skipped")
        load_provider.assert_not_called()

    def test_identity_preserved_at_full_strength_returns_the_candidate_untouched(self):
        source = np.full((200, 200, 3), 120, dtype=np.uint8)
        candidate = source.copy()
        patches = _patched(FakeDetector([(20, 20, 80, 80)]))
        with patches[0], patches[1]:
            output, metadata = apply_identity_guard(source, candidate)
        self.assertIs(output, candidate)
        self.assertEqual(metadata["status"], "preserved")
        self.assertFalse(metadata["applied"])
        self.assertEqual(metadata["strength"], 1.0)
        self.assertGreaterEqual(metadata["similarity"], 0.99)

    def test_identity_drift_reduces_strength_until_the_face_is_preserved(self):
        source = np.full((200, 200, 3), 50, dtype=np.uint8)
        candidate = np.full((200, 200, 3), 200, dtype=np.uint8)
        patches = _patched(FakeDetector([(20, 20, 80, 80)]))
        with patches[0], patches[1]:
            output, metadata = apply_identity_guard(source, candidate)
        self.assertEqual(metadata["status"], "strength_reduced")
        self.assertTrue(metadata["applied"])
        self.assertEqual(metadata["strength"], 0.4)
        self.assertLess(metadata["initial_similarity"], 0.60)
        self.assertGreaterEqual(metadata["similarity"], 0.60)
        self.assertAlmostEqual(float(output.mean()), 50 + 150 * 0.4, delta=1.0)
        self.assertEqual([attempt["strength"] for attempt in metadata["attempts"]], [1.0, 0.85, 0.7, 0.55, 0.4])

    @override_settings(IDENTITY_GUARD_STRENGTHS=(1.0, 0.85))
    def test_unresolved_when_no_strength_meets_the_threshold(self):
        source = np.full((200, 200, 3), 50, dtype=np.uint8)
        candidate = np.full((200, 200, 3), 200, dtype=np.uint8)
        patches = _patched(FakeDetector([(20, 20, 80, 80)]))
        with patches[0], patches[1]:
            output, metadata = apply_identity_guard(source, candidate)
        self.assertEqual(metadata["status"], "unresolved")
        self.assertTrue(metadata["applied"])
        self.assertEqual(metadata["strength"], 0.85)
        self.assertAlmostEqual(float(output.mean()), 50 + 150 * 0.85, delta=1.0)

    def test_the_worst_face_decides_when_several_faces_are_compared(self):
        source = np.full((200, 200, 3), 120, dtype=np.uint8)
        source[:, :100] = 50
        candidate = source.copy()
        candidate[:, :100] = 200
        boxes = [(10, 10, 80, 80), (110, 10, 80, 80)]
        patches = _patched(FakeDetector(boxes))
        with patches[0], patches[1]:
            _output, metadata = apply_identity_guard(source, candidate)
        self.assertEqual(metadata["faces_compared"], 2)
        self.assertEqual(metadata["status"], "strength_reduced")
        self.assertLess(metadata["initial_similarity"], 0.60)

    def test_dark_source_faces_are_not_judged(self):
        source = np.full((200, 200, 3), 20, dtype=np.uint8)
        candidate = np.full((200, 200, 3), 200, dtype=np.uint8)
        with (
            patch("enhancement.identity_guard._load_detector", return_value=FakeDetector([(20, 20, 80, 80)])),
            patch("enhancement.identity_guard._load_provider") as load_provider,
        ):
            output, metadata = apply_identity_guard(source, candidate)
        self.assertIs(output, candidate)
        self.assertEqual(metadata["status"], "skipped")
        self.assertIn("too dark", metadata["fallback_reason"])
        load_provider.assert_not_called()

    def test_a_face_that_cannot_be_redetected_is_inconclusive_and_left_unchanged(self):
        source = np.full((200, 200, 3), 120, dtype=np.uint8)
        candidate = np.full((200, 200, 3), 130, dtype=np.uint8)
        face = [(20, 20, 80, 80)]
        patches = _patched(FakeDetector(face, sequence=[face, []]))
        with patches[0], patches[1]:
            output, metadata = apply_identity_guard(source, candidate)
        self.assertIs(output, candidate)
        self.assertEqual(metadata["status"], "inconclusive")
        self.assertFalse(metadata["applied"])
        self.assertEqual(metadata["attempts"], [{"strength": 1.0, "similarity": None}])

    def test_a_face_missing_only_at_a_lower_strength_keeps_reducing(self):
        source = np.full((200, 200, 3), 50, dtype=np.uint8)
        candidate = np.full((200, 200, 3), 200, dtype=np.uint8)
        face = [(20, 20, 80, 80)]
        # source; drift at 1.0; face missing at 0.85; found again at every later
        # strength, where (with this fake provider) only 0.4 is close enough.
        patches = _patched(FakeDetector(face, sequence=[face, face, [], face]))
        with patches[0], patches[1]:
            _output, metadata = apply_identity_guard(source, candidate)
        self.assertLess(metadata["attempts"][0]["similarity"], 0.60)
        self.assertIsNone(metadata["attempts"][1]["similarity"])
        self.assertEqual(len(metadata["attempts"]), 5)
        self.assertEqual(metadata["strength"], 0.4)
        self.assertEqual(metadata["status"], "strength_reduced")

    def test_upscaled_candidates_are_compared_on_the_source_grid_and_keep_their_size(self):
        source = np.full((200, 200, 3), 50, dtype=np.uint8)
        candidate = np.full((400, 400, 3), 200, dtype=np.uint8)
        patches = _patched(FakeDetector([(20, 20, 80, 80)]))
        with patches[0], patches[1]:
            output, metadata = apply_identity_guard(source, candidate)
        self.assertEqual(output.shape, (400, 400, 3))
        self.assertEqual(metadata["status"], "strength_reduced")

    def test_an_unavailable_identity_model_raises_the_guard_unavailable_error(self):
        with patch("identities.providers.get_identity_provider") as get_provider:
            provider = Mock()
            provider.healthcheck.side_effect = ValidationError("model missing")
            get_provider.return_value = provider
            with self.assertRaises(IdentityGuardUnavailable):
                _load_provider()


PIPELINE_SETTINGS = dict(ADAPTIVE_DENOISE_ENABLED=False)


class IdentityGuardPipelineTests(SimpleTestCase):
    @override_settings(IDENTITY_GUARD_ENABLED=False, **PIPELINE_SETTINGS)
    @patch("enhancement.services.apply_identity_guard")
    def test_disabled_guard_never_runs(self, guard):
        source = np.full((80, 80, 3), 120, dtype=np.uint8)
        _output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(parameters["identity_guard"]["status"], "disabled")
        self.assertFalse(parameters["identity_guard"]["enabled"])
        self.assertIn("identity_guard", parameters["stage_order"])
        guard.assert_not_called()

    @override_settings(IDENTITY_GUARD_ENABLED=True, **PIPELINE_SETTINGS)
    @patch("enhancement.services.apply_identity_guard")
    def test_a_strength_reduction_replaces_the_output_and_is_auditable(self, guard):
        source = np.full((80, 80, 3), 120, dtype=np.uint8)
        blended = np.full((80, 80, 3), 99, dtype=np.uint8)
        guard.return_value = (
            blended,
            {"requested": True, "applied": True, "status": "strength_reduced", "similarity": 0.71, "strength": 0.7},
        )
        output, _level, parameters = enhance_bgr(source, "auto")
        np.testing.assert_array_equal(output, blended)
        self.assertEqual(parameters["identity_guard"]["status"], "strength_reduced")
        self.assertIn("+identity_guard", parameters["algorithm"])
        guard.assert_called_once()
        np.testing.assert_array_equal(guard.call_args.args[0], source)

    @override_settings(IDENTITY_GUARD_ENABLED=True, **PIPELINE_SETTINGS)
    @patch("enhancement.services.apply_identity_guard")
    def test_an_unresolved_result_adds_a_warning(self, guard):
        source = np.full((80, 80, 3), 120, dtype=np.uint8)
        guard.return_value = (
            source.copy(),
            {"requested": True, "applied": True, "status": "unresolved", "similarity": 0.3, "strength": 0.4},
        )
        _output, _level, parameters = enhance_bgr(source, "auto")
        self.assertTrue(any("identity guard" in warning for warning in parameters["warnings"]))

    @override_settings(IDENTITY_GUARD_ENABLED=True, **PIPELINE_SETTINGS)
    @patch("enhancement.services.apply_identity_guard")
    def test_unavailable_backends_skip_without_changing_the_output(self, guard):
        guard.side_effect = IdentityGuardUnavailable("The identity model is unavailable.")
        source = np.full((80, 80, 3), 120, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertEqual(parameters["identity_guard"]["status"], "skipped")
        self.assertIn("unavailable", parameters["identity_guard"]["fallback_reason"])
        self.assertFalse(parameters["identity_guard"]["applied"])

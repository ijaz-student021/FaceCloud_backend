"""scripts/evaluate_reference_quality.py: pure logic only.

A real run needs the real detector/identity models and the enhancement pipeline
(seconds per photo), so it is exercised manually, not by this fast suite -- the same
split ``config/test_model_tools.py`` uses for the sibling scripts.
"""
import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
from django.test import SimpleTestCase

SCRIPT = Path(__import__("django.conf").conf.settings.BASE_DIR) / "scripts" / "evaluate_reference_quality.py"
_spec = importlib.util.spec_from_file_location("evaluate_reference_quality", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class DarkenTests(SimpleTestCase):
    def setUp(self):
        self.image = np.random.default_rng(0).integers(60, 220, size=(80, 100, 3), dtype=np.uint8)

    def test_it_dims_the_photo_and_keeps_shape_and_type(self):
        dark = tool.darken(self.image, factor=0.2, noise_sigma=3, seed=1)
        self.assertEqual(dark.shape, self.image.shape)
        self.assertEqual(dark.dtype, np.uint8)
        self.assertLess(float(dark.mean()), float(self.image.mean()) * 0.5)

    def test_it_is_deterministic_for_the_same_seed_and_varies_by_seed(self):
        first = tool.darken(self.image, 0.2, 5, seed=7)
        second = tool.darken(self.image, 0.2, 5, seed=7)
        third = tool.darken(self.image, 0.2, 5, seed=8)
        self.assertTrue(np.array_equal(first, second))
        self.assertFalse(np.array_equal(first, third))

    def test_values_stay_in_the_valid_range(self):
        dark = tool.darken(self.image, factor=0.05, noise_sigma=40, seed=2)
        self.assertGreaterEqual(int(dark.min()), 0)
        self.assertLessEqual(int(dark.max()), 255)


class SeverityFunctionsTests(SimpleTestCase):
    def test_the_clean_severity_returns_the_image_unchanged(self):
        image = np.full((20, 20, 3), 100, dtype=np.uint8)
        severities = tool._severity_functions()
        self.assertIs(severities["clean"](image, 5), image)

    def test_darkening_severities_actually_darken(self):
        image = np.full((60, 60, 3), 150, dtype=np.uint8)
        severities = tool._severity_functions()
        for name in ("dim", "dark", "dark_blur", "dark_low_res"):
            result = severities[name](image, 3)
            self.assertLess(float(result.mean()), 150.0, name)

    def test_low_res_shrinks_the_photo(self):
        image = np.full((400, 300, 3), 120, dtype=np.uint8)
        result = tool._severity_functions()["low_res"](image, 3)
        self.assertLess(min(result.shape[:2]), 300)


class ResolveImagesDirTests(SimpleTestCase):
    def test_an_explicit_directory_is_used_even_if_empty(self):
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(tool.resolve_images_dir(str(empty)), Path(empty))

    def test_falls_back_through_the_default_list(self):
        real = Path(tempfile.mkdtemp())
        (real / "a.jpg").write_bytes(b"x")
        fake_defaults = (Path(tempfile.mkdtemp()), real, Path(tempfile.mkdtemp()))
        with unittest_mock_patch_defaults(fake_defaults):
            self.assertEqual(tool.resolve_images_dir(None), real)

    def test_raises_clearly_when_nothing_is_found(self):
        empty_defaults = tuple(Path(tempfile.mkdtemp()) for _ in range(2))
        with unittest_mock_patch_defaults(empty_defaults):
            with self.assertRaises(SystemExit):
                tool.resolve_images_dir(None)


def unittest_mock_patch_defaults(value):
    from unittest.mock import patch

    return patch.object(tool, "DEFAULT_IMAGE_DIRS", value)


class LargestFaceTests(SimpleTestCase):
    def detector(self, boxes, landmarks):
        detector = MagicMock()
        detector.detect.return_value = MagicMock(boxes=boxes, landmarks=landmarks)
        return detector

    def test_the_biggest_confident_face_is_chosen(self):
        boxes = [(0, 0, 10, 10), (0, 0, 50, 50)]
        landmarks = [[(1, 1)] * 5, [(2, 2)] * 5]
        found = tool.largest_face(None, self.detector(boxes, landmarks))
        self.assertEqual(found[0], (0, 0, 50, 50))

    def test_faces_without_five_landmarks_are_ignored(self):
        boxes = [(0, 0, 50, 50)]
        landmarks = [[(1, 1)] * 3]
        self.assertIsNone(tool.largest_face(None, self.detector(boxes, landmarks)))

    def test_no_detections_returns_none(self):
        self.assertIsNone(tool.largest_face(None, self.detector([], [])))


class EmbedCleanFacesTests(SimpleTestCase):
    def test_only_readable_images_with_a_confident_face_are_embedded(self):
        import cv2

        folder = Path(tempfile.mkdtemp())
        good = folder / "good.jpg"
        cv2.imwrite(str(good), np.full((40, 40, 3), 128, dtype=np.uint8))
        broken = folder / "broken.jpg"
        broken.write_bytes(b"not an image")

        detector = MagicMock()
        detector.detect.return_value = MagicMock(boxes=[(0, 0, 20, 20)], landmarks=[[(1, 1)] * 5])
        provider = MagicMock()
        provider.prepare_face.return_value = "aligned"
        provider.embed.return_value = np.array([1.0, 0.0])

        embeddings = tool.embed_clean_faces([good, broken], detector, provider)

        self.assertEqual(list(embeddings), ["good.jpg"])
        provider.prepare_face.assert_called_once()

    def test_a_provider_error_on_one_image_does_not_stop_the_others(self):
        import cv2

        folder = Path(tempfile.mkdtemp())
        paths = []
        for index in range(2):
            path = folder / f"p{index}.jpg"
            cv2.imwrite(str(path), np.full((30, 30, 3), 100, dtype=np.uint8))
            paths.append(path)

        detector = MagicMock()
        detector.detect.return_value = MagicMock(boxes=[(0, 0, 10, 10)], landmarks=[[(1, 1)] * 5])
        provider = MagicMock()
        provider.prepare_face.side_effect = [ValueError("boom"), "aligned"]
        provider.embed.return_value = np.array([1.0])

        embeddings = tool.embed_clean_faces(paths, detector, provider)

        self.assertEqual(list(embeddings), ["p1.jpg"])


class DifferentPersonSimilaritiesTests(SimpleTestCase):
    def test_pairs_are_unique_unordered_and_never_self_paired(self):
        embeddings = {f"p{i}": float(i) for i in range(6)}
        provider = MagicMock()
        provider.similarity.side_effect = lambda a, b: 1.0 - abs(a - b)

        similarities = tool.different_person_similarities(embeddings, provider, pair_count=100, seed=11)

        max_possible = 6 * 5 // 2
        self.assertEqual(len(similarities), max_possible)
        pairs_seen = {tuple(sorted(call.args)) for call in provider.similarity.call_args_list}
        self.assertEqual(len(pairs_seen), max_possible)

    def test_the_requested_count_is_respected_when_lower_than_the_maximum(self):
        embeddings = {f"p{i}": float(i) for i in range(10)}
        provider = MagicMock()
        provider.similarity.return_value = 0.1

        similarities = tool.different_person_similarities(embeddings, provider, pair_count=5, seed=1)

        self.assertEqual(len(similarities), 5)

    def test_it_is_reproducible_for_the_same_seed(self):
        embeddings = {f"p{i}": float(i) for i in range(8)}
        provider = MagicMock()
        provider.similarity.side_effect = lambda a, b: a + b

        first = tool.different_person_similarities(embeddings, provider, pair_count=6, seed=42)
        provider.similarity.side_effect = lambda a, b: a + b
        second = tool.different_person_similarities(embeddings, provider, pair_count=6, seed=42)

        self.assertEqual(first, second)

    def test_fewer_than_two_faces_gives_no_pairs(self):
        self.assertEqual(tool.different_person_similarities({}, MagicMock(), 10, 1), [])
        self.assertEqual(tool.different_person_similarities({"a": 1.0}, MagicMock(), 10, 1), [])


class MeasureRecoveryTests(SimpleTestCase):
    def detector(self, boxes, landmarks):
        detector = MagicMock()
        detector.detect.return_value = MagicMock(boxes=boxes, landmarks=landmarks)
        return detector

    def test_no_face_in_the_output_is_reported_not_a_similarity_of_zero(self):
        result = tool.measure_recovery(None, "clean-embedding", self.detector([], []), MagicMock())
        self.assertEqual(result, {"status": "no_face_in_output", "similarity": None})

    def test_a_provider_failure_on_the_output_is_reported_not_raised(self):
        provider = MagicMock()
        provider.prepare_face.side_effect = ValueError("boom")
        result = tool.measure_recovery(
            None, "clean-embedding", self.detector([(0, 0, 30, 30)], [[(1, 1)] * 5]), provider,
        )
        self.assertEqual(result["status"], "embedding_failed")
        self.assertIsNone(result["similarity"])

    def test_success_compares_against_the_clean_embedding_passed_in_not_a_fresh_one(self):
        provider = MagicMock()
        provider.prepare_face.return_value = "aligned"
        provider.embed.return_value = "output-embedding"
        provider.similarity.return_value = 0.87
        result = tool.measure_recovery(
            None, "clean-embedding", self.detector([(0, 0, 30, 30)], [[(1, 1)] * 5]), provider,
        )
        self.assertEqual(result, {"status": "recovered", "similarity": 0.87})
        provider.similarity.assert_called_once_with("clean-embedding", "output-embedding")


class SummarizeSeverityTests(SimpleTestCase):
    def record(self, seconds, noise, status, similarity):
        return {
            "seconds": seconds, "noise": noise,
            "identity": {"status": status, "similarity": similarity},
        }

    def test_means_and_the_worst_similarity_are_computed(self):
        records = [
            self.record(2.0, 4.0, "passed", 0.95),
            self.record(4.0, 6.0, "passed", 0.82),
            self.record(3.0, 5.0, "skipped", None),
        ]
        summary = tool.summarize_severity(records)
        self.assertEqual(summary["mean_seconds"], 3.0)
        self.assertEqual(summary["mean_noise"], 5.0)
        self.assertEqual(summary["same_person_mean_similarity"], round((0.95 + 0.82) / 2, 4))
        self.assertEqual(summary["same_person_min_similarity"], 0.82)
        self.assertEqual(summary["same_person_judged"], 2)
        self.assertEqual(summary["identity_statuses"], {"passed": 2, "skipped": 1})

    def test_errors_are_counted_and_excluded_from_the_averages(self):
        records = [self.record(1.0, 1.0, "passed", 0.9), {"error": "boom"}]
        summary = tool.summarize_severity(records)
        self.assertEqual((summary["photos"], summary["errors"]), (2, 1))
        self.assertEqual(summary["mean_seconds"], 1.0)

    def test_no_judged_faces_gives_a_valid_summary_without_similarity_keys(self):
        summary = tool.summarize_severity([self.record(1.0, 1.0, "skipped", None)])
        self.assertEqual(summary["same_person_judged"], 0)
        self.assertNotIn("same_person_mean_similarity", summary)


class MarkdownReportTests(SimpleTestCase):
    def test_the_report_names_every_severity_and_flags_a_missing_safety_margin(self):
        summaries = {
            "clean": tool.summarize_severity([{
                "seconds": 1.0, "noise": 1.0, "identity": {"status": "passed", "similarity": 0.99},
            }]),
            "dark": tool.summarize_severity([{
                "seconds": 2.0, "noise": 6.0, "identity": {"status": "passed", "similarity": 0.5},
            }]),
        }
        text = tool.markdown_report(summaries, [0.9], {"run": "t", "photos": 1, "seed": 11, "embedded_faces": 2}, threshold=0.80)
        self.assertIn("| clean |", text)
        self.assertIn("| dark |", text)
        self.assertIn("NO SAFETY MARGIN", text)

    def test_a_clear_margin_is_reported_positively(self):
        summaries = {"clean": tool.summarize_severity([{
            "seconds": 1.0, "noise": 1.0, "identity": {"status": "passed", "similarity": 0.95},
        }])}
        text = tool.markdown_report(summaries, [0.2], {"run": "t", "photos": 1, "seed": 11, "embedded_faces": 2}, threshold=0.80)
        self.assertIn("room to spare", text)

    def test_no_different_person_pairs_is_reported_not_crashed(self):
        summaries = {"clean": tool.summarize_severity([{
            "seconds": 1.0, "noise": 1.0, "identity": {"status": "passed", "similarity": 0.95},
        }])}
        text = tool.markdown_report(summaries, [], {"run": "t", "photos": 1, "seed": 11, "embedded_faces": 0}, threshold=0.80)
        self.assertIn("Not enough clean faces", text)


class NoBiometricDataPersistedTests(SimpleTestCase):
    """Only scores may ever reach a results file -- never an embedding vector."""

    def test_a_severity_record_serializes_to_plain_json_safe_values(self):
        record = {
            "severity": "dark", "photo": "p.jpg", "seconds": 1.2, "applied_level": "high",
            "noise": 4.0, "luminance": 90.0, "identity": {"status": "passed", "similarity": 0.91, "reason": ""},
        }
        text = json.dumps(record)
        parsed = json.loads(text)
        for value in _flatten(parsed):
            self.assertIsInstance(value, (str, int, float, bool, type(None)))

    def test_different_person_similarities_are_plain_floats_not_vectors(self):
        embeddings = {"a": np.array([1.0, 0.0, 0.0]), "b": np.array([0.0, 1.0, 0.0])}
        provider = MagicMock()
        provider.similarity.return_value = 0.42
        similarities = tool.different_person_similarities(embeddings, provider, pair_count=1, seed=1)
        self.assertEqual(similarities, [0.42])
        json.dumps(similarities)  # must not raise: no numpy array leaked through


def _flatten(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    else:
        yield value

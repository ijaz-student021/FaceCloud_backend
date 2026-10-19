"""The identity limits per provider, the one grouping rule, and the evaluation that the limits come from."""
import json
import math
import os
import random
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.core.checks import Error
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from . import threshold_evaluation as evaluation
from .checks import identity_configuration_check
from .management.commands.evaluate_identity_thresholds import labelled_images
from .providers import (ArcFaceOnnxProvider, DisabledIdentityProvider, IdentityEmbeddingProvider, SFaceOnnxProvider, merge_suggestion_minimum,
                        thresholds_for)
from .services import decide_group


class LimitsPerProviderTests(SimpleTestCase):
    def test_sface_has_limits_of_its_own_not_the_generic_ones(self):
        limits = thresholds_for(SFaceOnnxProvider())
        self.assertEqual(limits, {"guard": 0.80, "match": 0.45, "new_group": 0.30, "margin": 0.06})
        self.assertLess(limits["new_group"], limits["match"])
        generic = thresholds_for(type("Stand", (IdentityEmbeddingProvider,), {"name": "x", "healthcheck": lambda self: True, "embed": lambda self, f: f})())
        self.assertEqual((generic["new_group"], generic["match"]), (0.68, 0.82))             # a provider with none of its own still falls back to these

    def test_each_limit_can_be_set_from_the_settings(self):
        with override_settings(SFACE_NEW_GROUP_THRESHOLD=0.2, SFACE_MATCH_THRESHOLD=0.5, SFACE_AMBIGUITY_MARGIN=0.1, SFACE_GUARD_MIN_SIMILARITY=0.7):
            self.assertEqual(thresholds_for(SFaceOnnxProvider()), {"guard": 0.7, "match": 0.5, "new_group": 0.2, "margin": 0.1})

    def test_arcface_keeps_its_own_and_the_generic_settings_no_longer_move_sface(self):
        arcface = thresholds_for(ArcFaceOnnxProvider())
        self.assertEqual((arcface["new_group"], arcface["match"]), (0.35, 0.60))
        with override_settings(IDENTITY_MATCH_THRESHOLD=0.99, IDENTITY_NEW_GROUP_THRESHOLD=0.98):
            self.assertEqual(thresholds_for(SFaceOnnxProvider())["match"], 0.45)

    def test_a_subclass_that_sets_its_own_limit_keeps_it(self):
        class Own(SFaceOnnxProvider):
            match_threshold = 0.61

        self.assertEqual(thresholds_for(Own())["match"], 0.61)
        self.assertEqual(thresholds_for(Own())["new_group"], 0.30)

    def test_the_merge_suggestion_is_on_the_providers_scale_unless_a_number_is_set(self):
        self.assertEqual(merge_suggestion_minimum(SFaceOnnxProvider()), 0.40)
        self.assertEqual(merge_suggestion_minimum(ArcFaceOnnxProvider()), 0.60)                 # no figure of its own: its match limit
        self.assertEqual(merge_suggestion_minimum(DisabledIdentityProvider()), 0.82)
        with override_settings(MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY=0.95):
            self.assertEqual(merge_suggestion_minimum(SFaceOnnxProvider()), 0.95)

    def test_the_system_check_refuses_sface_limits_that_are_out_of_order(self):
        def ids():
            return {error.id for error in identity_configuration_check(None) if isinstance(error, Error)}

        self.assertNotIn("identities.E017", ids())
        with override_settings(SFACE_NEW_GROUP_THRESHOLD=0.5, SFACE_MATCH_THRESHOLD=0.4):
            self.assertIn("identities.E017", ids())
        with override_settings(SFACE_AMBIGUITY_MARGIN=1.0):
            self.assertIn("identities.E018", ids())


class OneGroupingRuleTests(SimpleTestCase):
    LIMITS = {"new_group": 0.30, "match": 0.45, "margin": 0.06}

    def test_below_new_group_a_folder_opens_even_when_the_old_limit_would_have_held_it_back(self):
        self.assertEqual(decide_group([], self.LIMITS)[0], "new")
        self.assertEqual(decide_group([(0.29, "a")], self.LIMITS)[0], "new")
        self.assertEqual(decide_group([(0.30, "a")], self.LIMITS)[:2], ("review", "a"))

    def test_a_face_joins_only_at_the_match_limit_and_clear_of_the_runner_up(self):
        self.assertEqual(decide_group([(0.45, "a")], self.LIMITS)[:2], ("match", "a"))
        self.assertEqual(decide_group([(0.44, "a")], self.LIMITS)[0], "review")                 # alike, not sure enough: a person decides
        self.assertEqual(decide_group([(0.60, "a"), (0.58, "b")], self.LIMITS)[0], "review")    # two folders as close as each other
        self.assertEqual(decide_group([(0.60, "a"), (0.50, "b")], self.LIMITS)[:2], ("match", "a"))

    def test_the_old_limits_would_have_opened_a_folder_for_a_face_that_is_clearly_this_person(self):
        old = {"new_group": 0.68, "match": 0.82, "margin": 0.06}
        self.assertEqual(decide_group([(0.55, "a")], old)[0], "new")
        self.assertEqual(decide_group([(0.55, "a")], self.LIMITS)[0], "match")


class EvaluationTests(SimpleTestCase):
    def people(self, count=8, photos=12, spread=14.0, seed=0):
        """Unit vectors for ``count`` people (well separated axes in 16 dimensions) with ``photos`` noisy photographs each."""
        rng = np.random.default_rng(seed)
        centres = np.eye(16, dtype=np.float32)[:count]
        vectors, labels = [], []
        for person, centre in enumerate(centres):
            for _ in range(photos):
                noise = rng.normal(0, math.tan(math.radians(spread)) / 4.0, 16).astype(np.float32)
                vector = centre + noise
                vectors.append(vector / np.linalg.norm(vector))
                labels.append(f"p{person}")
        return np.asarray(vectors), labels

    def test_same_and_different_person_pairs_are_counted_and_described(self):
        vectors, labels = self.people(count=4, photos=5)
        same, different = evaluation.pair_similarities(vectors, labels)
        self.assertEqual(len(same), 4 * 10)                       # 5 photographs: 10 pairs a person
        self.assertEqual(len(different), 6 * 25)                  # 4 people: 6 pairs of people, 5 x 5 photographs each
        self.assertGreater(same.mean(), different.mean() + 0.5)
        summary = evaluation.describe(same)
        self.assertEqual(summary["count"], 40)
        self.assertLessEqual(summary["min"], summary["p50"])
        self.assertLessEqual(summary["p50"], summary["max"])
        self.assertEqual(evaluation.describe([]), {"count": 0})

    def test_different_person_pairs_are_sampled_when_there_are_too_many(self):
        vectors, labels = self.people(count=6, photos=10)
        _same, different = evaluation.pair_similarities(vectors, labels, max_impostor_pairs=100, rng=random.Random(1))
        self.assertEqual(len(different), 100)

    def test_operating_points_and_the_threshold_at_a_false_accept_rate(self):
        same, different = np.array([0.5, 0.6, 0.7, 0.8]), np.array([0.0, 0.1, 0.2, 0.55])
        points = {point["threshold"]: point for point in evaluation.operating_points(same, different, [0.0, 0.5, 0.65])}
        self.assertEqual((points[0.5]["tar"], points[0.5]["far"]), (1.0, 0.25))
        self.assertEqual((points[0.65]["tar"], points[0.65]["far"]), (0.5, 0.0))
        self.assertGreaterEqual(evaluation.threshold_at_far(different, 0.0), 0.55 - 1e-9)       # no different-person pair may get through: the highest one
        self.assertLess(evaluation.threshold_at_far(different, 0.5), 0.2)

    def test_the_recommended_limits_follow_from_the_two_distributions(self):
        same = np.linspace(0.31, 0.90, 200)                                   # 1st percentile just above 0.31
        different = np.linspace(-0.20, 0.40, 2000)                            # 99.99th percentile about 0.40
        pair = evaluation.recommend_limits(same, different, step=0.05)
        self.assertEqual(pair["new_group"], 0.30)                             # rounded DOWN to the grid
        self.assertEqual(pair["match"], 0.40)                                 # rounded UP to the grid
        close = evaluation.recommend_limits(np.full(50, 0.42), np.linspace(-0.2, 0.30, 500), step=0.05)
        self.assertGreaterEqual(close["match"], close["new_group"] + 0.05)    # always a band for review in between

    def test_grouping_simulated_with_the_services_rule_puts_each_person_in_one_folder(self):
        vectors, labels = self.people(count=3, photos=8, spread=10.0)
        order = list(range(len(labels)))
        random.Random(3).shuffle(order)
        assignments, centroids = evaluation.simulate_grouping([vectors[i] for i in order], {"new_group": 0.3, "match": 0.5, "margin": 0.05})
        metrics = evaluation.album_metrics([labels[i] for i in order], assignments)
        self.assertEqual(metrics["folders"], 3)
        self.assertEqual(len(centroids), 3)
        self.assertEqual(metrics["wrong_confirmed_rate"], 0.0)
        self.assertEqual(metrics["pair_precision"], 1.0)
        self.assertEqual(metrics["pair_recall"], 1.0)

    def test_too_strict_old_style_limits_split_a_person_and_the_metrics_say_so(self):
        vectors, labels = self.people(count=3, photos=8, spread=25.0)
        order = list(range(len(labels)))
        random.Random(4).shuffle(order)
        assignments, _ = evaluation.simulate_grouping([vectors[i] for i in order], {"new_group": 0.95, "match": 0.99, "margin": 0.0})
        metrics = evaluation.album_metrics([labels[i] for i in order], assignments)
        self.assertGreater(metrics["folders_per_person"], 2.0)

    def test_candidates_are_compared_on_the_same_albums_and_the_recommended_pair_is_checked(self):
        vectors, labels = self.people(count=8, photos=12, spread=12.0)
        pair = {"new_group": 0.30, "match": 0.50}
        rows = evaluation.evaluate_candidates(vectors, labels, [(0.30, 0.50), (0.95, 0.99)], 0.05, people=4, per_person=(6, 10), albums=12)
        self.assertEqual([(row["new_group"], row["match"]) for row in rows], [(0.30, 0.50), (0.95, 0.99)])
        self.assertLess(rows[0]["folders_per_person"], rows[1]["folders_per_person"])
        row, verdict = evaluation.recommend(rows, pair)
        self.assertIn("meets the safety limits", verdict)
        _row, verdict = evaluation.recommend(rows, pair, max_wrong=-1.0)                    # a limit nothing can meet
        self.assertIn("does NOT meet", verdict)
        with self.assertRaises(ValueError):
            evaluation.recommend(rows, {"new_group": 0.1, "match": 0.2})

    def test_an_album_needs_enough_people_with_enough_photographs(self):
        vectors, labels = self.people(count=2, photos=3)
        with self.assertRaises(ValueError):
            evaluation.evaluate_candidates(vectors, labels, [(0.3, 0.5)], 0.05, people=4, per_person=(6, 10), albums=2)

    def test_folder_pair_similarity_separates_folders_of_one_person_from_folders_of_two(self):
        vectors, labels = self.people(count=8, photos=12, spread=30.0)
        same, different = evaluation.folder_pair_similarities(vectors, labels, {"new_group": 0.9, "match": 0.95, "margin": 0.0}, albums=10, people=4, per_person=(6, 10))
        self.assertGreater(len(same), 0)
        self.assertGreater(float(np.mean(same)), float(np.mean(different)))


class EvaluationCommandTests(SimpleTestCase):
    def folder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_the_three_ways_to_label_photographs(self):
        folder = self.folder()
        for name in ("anna__1.jpg", "anna__2.jpg", "ben__1.png", "notes.txt", "loose.jpg"):
            (folder / name).write_bytes(b"x")
        (folder / "carl").mkdir()
        (folder / "carl" / "any_name.jpeg").write_bytes(b"x")
        found = {(path.name, person) for path, person in labelled_images(folder)}
        self.assertEqual(found, {("anna__1.jpg", "anna"), ("anna__2.jpg", "anna"), ("ben__1.png", "ben"), ("any_name.jpeg", "carl")})
        (folder / "labels.csv").write_text("image,person\nloose.jpg,dora\nanna__1.jpg,anna\n", encoding="utf-8")
        listed = [(path.name, person) for path, person in labelled_images(folder, folder / "labels.csv")]
        self.assertEqual(listed, [("loose.jpg", "dora"), ("anna__1.jpg", "anna")])

    def test_the_command_reads_photographs_embeds_them_and_reports_without_touching_the_database(self):
        folder = self.folder()
        rng = np.random.default_rng(5)
        vectors = {}
        for person in range(8):
            centre = np.eye(16, dtype=np.float32)[person]
            for number in range(8):
                name = f"person{person}__{number}.jpg"
                (folder / name).write_bytes(b"not read: the detector below is a stand-in")
                noise = rng.normal(0, 0.05, 16).astype(np.float32)
                vector = centre + noise
                vectors[name] = vector / np.linalg.norm(vector)

        class Provider(IdentityEmbeddingProvider):
            name, dimension, model_checksum = "stand_in", 16, "abc"
            new_group_threshold, match_threshold, ambiguity_margin, guard_min_similarity = 0.30, 0.45, 0.06, 0.80

            def healthcheck(self):
                return True

            def prepare_face(self, image, box, landmarks=None):
                return image

            def embed(self, prepared_face):
                return prepared_face

        current = {"name": None}

        def read(path):
            current["name"] = Path(path).name
            return vectors[current["name"]].reshape(4, 4, 1)               # the "image" is the vector itself

        class Detector:
            pass

        def largest_face(image, detector):
            return (0, 0, 4, 4), [(0, 0)] * 5

        class StandIn(Provider):
            def prepare_face(self, image, box, landmarks=None):
                return vectors[current["name"]]

        out, json_path = StringIO(), str(folder / "result.json")
        with patch("identities.management.commands.evaluate_identity_thresholds.get_identity_provider", return_value=StandIn()), \
                patch("identities.management.commands.evaluate_identity_thresholds.get_face_detector", return_value=Detector()), \
                patch("identities.management.commands.evaluate_identity_thresholds.cv2.imread", side_effect=read), \
                patch("identities.management.commands.evaluate_identity_thresholds._largest_face", side_effect=largest_face):
            call_command("evaluate_identity_thresholds", str(folder), "--albums", "6", "--album-people", "4", "--album-photos", "6,8", "--json", json_path, stdout=out)
        text = out.getvalue()
        for expected in ("Cosine similarity of pairs", "same person", "different people", "Derived limits", "Simulated albums", "Recommended:", "merge suggestion compares"):
            self.assertIn(expected, text)
        result = json.loads(Path(json_path).read_text(encoding="utf-8"))
        self.assertEqual(result["counts"]["embedded"], 64)
        self.assertEqual(result["same_person"]["count"], 8 * 28)
        self.assertGreater(result["same_person"]["mean"], result["different_people"]["mean"])
        self.assertIn("derived_limits", result)
        self.assertTrue(os.path.exists(json_path))

    def test_it_says_what_is_wrong_when_there_is_nothing_to_measure(self):
        folder = self.folder()

        class Provider(SFaceOnnxProvider):
            def healthcheck(self):
                return True

        with patch("identities.management.commands.evaluate_identity_thresholds.get_identity_provider", return_value=Provider()), \
                patch("identities.management.commands.evaluate_identity_thresholds.get_face_detector"):
            with self.assertRaisesMessage(CommandError, "No photographs found"):
                call_command("evaluate_identity_thresholds", str(folder))

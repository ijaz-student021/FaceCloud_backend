"""Measure how well the configured face-embedding model separates people, and which grouping limits follow from it.

    python manage.py evaluate_identity_thresholds /path/to/photos

The folder holds photographs named ``<person>__<n>.jpg`` (``anna__1.jpg``, ``anna__2.jpg``, ``ben__1.jpg``), or one sub-folder per person, or any layout
described by ``--csv labels.csv`` (columns ``image,person``, paths relative to the folder). Every photograph is run through the same face detector, alignment
and embedding model the application uses, then:

1. the similarity of every pair of photographs of the same person and of different people is summarised, with how many of each a threshold lets through;
2. albums of a few people are simulated through the application's own grouping rule for each candidate pair of limits, reporting how many folders a person
   ends up in, how many confirmed photographs land in someone else's folder and how many wait for review; and one pair is recommended.

It reads files and loads the model; it writes nothing to the database. The photographs stay where they are.
"""
import csv
import json
import random
from pathlib import Path

import cv2
import numpy as np
from django.core.management.base import BaseCommand, CommandError

from identities import threshold_evaluation as evaluation
from identities.providers import get_identity_provider, thresholds_for
from identities.services import _largest_face
from validation.services import get_face_detector

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def _floats(text):
    return [float(part) for part in text.split(",") if part.strip()]


def labelled_images(folder, csv_path=None):
    """``[(path, person)]`` from a CSV, ``<person>__<n>.<ext>`` names in the folder, or one sub-folder per person."""
    folder = Path(folder)
    if csv_path:
        with open(csv_path, newline="", encoding="utf-8") as handle:
            return [(folder / row["image"], row["person"]) for row in csv.DictReader(handle)]
    found = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
            continue
        if path.parent != folder:
            found.append((path, path.parent.name))
        elif "__" in path.stem:
            found.append((path, path.stem.split("__", 1)[0]))
    return found


class Command(BaseCommand):
    help = "Measure same-person and different-person similarity for the configured identity model and recommend grouping limits (writes nothing)."

    def add_arguments(self, parser):
        parser.add_argument("folder", help="Photographs named <person>__<n>.jpg, or one sub-folder per person.")
        parser.add_argument("--csv", help="A CSV with the columns image,person (image paths relative to the folder).")
        parser.add_argument("--max-people", type=int, default=400, help="Use at most this many people (random, seeded). Default 400.")
        parser.add_argument("--max-per-person", type=int, default=20, help="Use at most this many photographs of one person. Default 20.")
        parser.add_argument("--albums", type=int, default=60, help="Simulated albums per candidate. Default 60.")
        parser.add_argument("--album-people", type=int, default=6, help="People in one simulated album. Default 6.")
        parser.add_argument("--album-photos", default="6,14", help="Photographs of one person in an album: MIN,MAX. Default 6,14.")
        parser.add_argument("--new-group-values", default="0.10,0.15,0.20,0.25,0.30,0.35,0.40,0.45,0.50")
        parser.add_argument("--match-values", default="0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60,0.65,0.70")
        parser.add_argument("--margin", type=float, default=None, help="The ambiguity margin to test (default: the provider's).")
        parser.add_argument("--max-wrong", type=float, default=0.01, help="Most confirmed photographs allowed in the wrong folder. Default 0.01.")
        parser.add_argument("--max-review", type=float, default=0.30, help="Most photographs allowed to wait for review. Default 0.30.")
        parser.add_argument("--step", type=float, default=0.05, help="The grid the derived limits are rounded to. Default 0.05.")
        parser.add_argument("--new-group-percentile", type=float, default=1.0, help="new_group = this percentile of same-person similarities (rounded down). Default 1.")
        parser.add_argument("--match-far", type=float, default=0.0001, help="match = the threshold at most this share of different-person pairs reach (rounded up). Default 0.0001.")
        parser.add_argument("--seed", type=int, default=0)
        parser.add_argument("--cache", help="An .npz file: reuse embeddings from it, or write them there after computing.")
        parser.add_argument("--json", dest="json_path", help="Also write the full result to this JSON file.")

    def handle(self, *args, **options):
        provider = get_identity_provider()
        try:
            provider.healthcheck()
        except Exception as error:      # a ValidationError names the problem in plain words
            raise CommandError(f"The identity model is not usable here: {getattr(error, 'detail', error)}") from error
        vectors, labels, counts = self._embeddings(provider, options)
        if len(set(labels)) < max(2, options["album_people"]):
            raise CommandError(f"Only {len(set(labels))} people could be embedded; need at least {options['album_people']} for the albums.")
        current = thresholds_for(provider)
        margin = options["margin"] if options["margin"] is not None else current["margin"]
        same, different = evaluation.pair_similarities(vectors, labels, rng=random.Random(options["seed"]))
        report = {
            "provider": provider.name, "model_checksum": provider.model_checksum, "counts": counts,
            "current_limits": current, "same_person": evaluation.describe(same), "different_people": evaluation.describe(different),
        }
        self._print_distributions(report, same, different)

        low, high = (int(part) for part in options["album_photos"].split(","))
        pair = evaluation.recommend_limits(same, different, step=options["step"], new_group_percentile=options["new_group_percentile"], match_far=options["match_far"])
        report["derived_limits"] = pair
        candidates = [(n, m) for n in _floats(options["new_group_values"]) for m in _floats(options["match_values"]) if m >= n]
        for extra in ((pair["new_group"], pair["match"]), (current["new_group"], current["match"])):
            if extra not in candidates:
                candidates.append(extra)
        try:
            rows = evaluation.evaluate_candidates(
                vectors, labels, candidates, margin,
                people=options["album_people"], per_person=(low, high), albums=options["albums"], seed=options["seed"],
            )
        except ValueError as error:
            raise CommandError(str(error)) from error
        report["albums"] = {"people": options["album_people"], "photos_per_person": [low, high], "albums": options["albums"], "margin": margin}
        report["candidates"] = rows
        today = next(row for row in rows if (row["new_group"], row["match"]) == (current["new_group"], current["match"]))
        best, why = evaluation.recommend(rows, pair, options["max_wrong"], options["max_review"])
        report["recommended"] = {**best, "reason": why}
        self._print_candidates(rows, today, best, why, pair)

        for name, limits in (("recommended", {"new_group": best["new_group"], "match": best["match"], "margin": margin}),
                             ("in use now", {"new_group": current["new_group"], "match": current["match"], "margin": margin})):
            folders_same, folders_different = evaluation.folder_pair_similarities(
                vectors, labels, limits, albums=options["albums"], people=options["album_people"], per_person=(low, high), seed=options["seed"],
            )
            report[f"folder_pairs_{name.replace(' ', '_')}"] = {"limits": limits, "same_person": evaluation.describe(folders_same), "different_people": evaluation.describe(folders_different)}
            self._print_folder_pairs(name, limits, folders_same, folders_different)
        if options["json_path"]:
            Path(options["json_path"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
            self.stdout.write(f"\nFull result written to {options['json_path']}")

    # -- embeddings ---------------------------------------------------------------------------------------------------
    def _embeddings(self, provider, options):
        cache = Path(options["cache"]) if options.get("cache") else None
        if cache is not None and cache.exists():
            data = np.load(cache, allow_pickle=False)
            if str(data["provider"]) == provider.name and str(data["checksum"]) == provider.model_checksum:
                self.stdout.write(f"Embeddings reused from {cache}.")
                return data["vectors"], [str(label) for label in data["labels"]], json.loads(str(data["counts"]))
        found = labelled_images(options["folder"], options["csv"])
        if not found:
            raise CommandError("No photographs found. Name them <person>__<n>.jpg, put them in one sub-folder per person, or pass --csv.")
        rng = random.Random(options["seed"])
        by_person = {}
        for path, person in found:
            by_person.setdefault(person, []).append(path)
        # People with enough photographs for an album first (they make the same-person pairs and the albums), then the rest (more different-person pairs).
        album_minimum = int(options["album_photos"].split(",")[0])
        people = sorted(by_person)
        rng.shuffle(people)
        people.sort(key=lambda person: len(by_person[person]) < album_minimum)
        chosen = [(path, person) for person in people[: options["max_people"]] for path in sorted(by_person[person])[: options["max_per_person"]]]
        detector = get_face_detector()
        vectors, labels = [], []
        counts = {"listed": len(found), "tried": len(chosen), "no_face": 0, "unreadable": 0, "embedding_failed": 0}
        for position, (path, person) in enumerate(chosen, 1):
            image = cv2.imread(str(path))
            if image is None:
                counts["unreadable"] += 1
                continue
            face = _largest_face(image, detector)
            if face is None:
                counts["no_face"] += 1
                continue
            box, landmarks = face
            x, y, width, height = box
            try:
                crop = provider.prepare_face(image, {"x": x, "y": y, "width": width, "height": height}, landmarks)
                vectors.append(provider.validate_embedding(provider.embed(crop)))
                labels.append(person)
            except Exception:
                counts["embedding_failed"] += 1
            if position % 250 == 0:
                self.stdout.write(f"  embedded {position} of {len(chosen)} photographs...")
        # A person with one usable photograph gives no same-person pair, but still different-person pairs.
        counts["embedded"] = len(vectors)
        counts["people"] = len(set(labels))
        vectors = np.asarray(vectors, dtype=np.float32)
        if cache is not None:
            np.savez_compressed(cache, vectors=vectors, labels=np.asarray(labels), provider=provider.name, checksum=provider.model_checksum, counts=json.dumps(counts))
        return vectors, labels, counts

    # -- printing -----------------------------------------------------------------------------------------------------
    def _print_distributions(self, report, same, different):
        out = self.stdout.write
        counts = report["counts"]
        out(f"Provider: {report['provider']}")
        out(f"Photographs: {counts['embedded']} embedded of {counts['tried']} tried ({counts.get('no_face', 0)} without a usable face, "
            f"{counts.get('unreadable', 0)} unreadable, {counts.get('embedding_failed', 0)} failed), {counts['people']} people")
        out(f"Limits in use now: {report['current_limits']}")
        out("\nCosine similarity of pairs")
        header = f"{'':20}{'pairs':>10}{'mean':>8}{'std':>7}{'min':>7}{'1%':>7}{'5%':>7}{'25%':>7}{'50%':>7}{'75%':>7}{'95%':>7}{'99%':>7}{'max':>7}"
        out(header)
        for name, summary in (("same person", report["same_person"]), ("different people", report["different_people"])):
            if not summary["count"]:
                out(f"{name:20}{0:>10}")
                continue
            out(f"{name:20}{summary['count']:>10}{summary['mean']:>8.3f}{summary['std']:>7.3f}{summary['min']:>7.3f}{summary['p1']:>7.3f}{summary['p5']:>7.3f}"
                f"{summary['p25']:>7.3f}{summary['p50']:>7.3f}{summary['p75']:>7.3f}{summary['p95']:>7.3f}{summary['p99']:>7.3f}{summary['max']:>7.3f}")
        out("\nA threshold lets through this share of same-person pairs (tar) and of different-person pairs (far)")
        out(f"{'threshold':>10}{'tar':>9}{'far':>10}")
        for point in evaluation.operating_points(same, different, [round(t, 2) for t in np.arange(0.0, 0.85, 0.05)]):
            out(f"{point['threshold']:>10.2f}{point['tar']:>9.3f}{point['far']:>10.4f}")
        for far in (0.01, 0.001, 0.0001):
            out(f"Threshold with at most {far:.2%} of different-person pairs through: {evaluation.threshold_at_far(different, far):.3f}")

    def _print_candidates(self, rows, today, best, why, pair):
        out = self.stdout.write
        out(f"\nDerived limits: new_group {pair['new_group']} (the {pair['same_person_percentile']:g}th percentile of same-person pairs is {pair['same_person_floor']:.3f}), "
            f"match {pair['match']} (at most {pair['match_far']:.2%} of different-person pairs reach {pair['different_people_ceiling']:.3f})")
        out("\nSimulated albums (each row: the same albums, one pair of limits)")
        out(f"{'new_group':>9}{'match':>7}{'folders/person':>16}{'wrong placed':>14}{'to review':>11}{'review hint ok':>16}{'pair prec':>11}{'pair recall':>13}")
        for row in rows:
            hint = "-" if row["review_suggestion_correct_rate"] is None else f"{row['review_suggestion_correct_rate']:.2f}"
            marker = "  <- recommended" if row is best else ""
            out(f"{row['new_group']:>9.2f}{row['match']:>7.2f}{row['folders_per_person']:>16.2f}{row['wrong_confirmed_rate']:>14.3f}{row['review_rate']:>11.2f}"
                f"{hint:>16}{row['pair_precision']:>11.3f}{row['pair_recall']:>13.3f}{marker}")
        out(f"\nLimits in use now (new_group {today['new_group']}, match {today['match']}): {today['folders_per_person']:.2f} folders per person, "
            f"{today['wrong_confirmed_rate']:.3f} wrong, {today['review_rate']:.2f} to review")
        out(f"Recommended: new_group {best['new_group']}, match {best['match']}, margin {best['margin']}: {why}")

    def _print_folder_pairs(self, name, limits, same, different):
        out = self.stdout.write
        out(f"\nAverage-face similarity between two folders, at the limits {name} (new_group {limits['new_group']}, match {limits['match']}): what the merge suggestion compares")
        for name, values in (("folders of one person", same), ("folders of different people", different)):
            summary = evaluation.describe(values)
            if not summary["count"]:
                out(f"{name:30} none")
                continue
            out(f"{name:30}{summary['count']:>7} pairs  mean {summary['mean']:.3f}  p5 {summary['p5']:.3f}  p50 {summary['p50']:.3f}  p95 {summary['p95']:.3f}  "
                f"p99 {summary['p99']:.3f}  max {summary['max']:.3f}")

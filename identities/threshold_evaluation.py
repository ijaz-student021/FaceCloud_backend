"""Measuring how well a face-embedding model's similarity separates people, and what grouping limits follow from it.

Used by ``manage.py evaluate_identity_thresholds``. Nothing here touches the database or a model: it works on unit vectors and labels, so the
numbers it prints can be reproduced and the code tested with made-up vectors.

Two measurements:

* **Pair distributions** - the cosine similarity of every pair of photographs of the *same* person and of *different* people, and for a list of
  thresholds how many of each side a threshold lets through (``tar``: same-person pairs at or above it; ``far``: different-person pairs at or above it).
* **Album simulation** - what ``identities.services`` would actually do. Photographs of a few people arrive in random order and each one is placed with
  ``decide_group`` (the very function the service uses) against the average face of the folders so far, for each candidate pair of limits. Per candidate:
  how many folders one person ends up in, how many confirmed photographs sit in a folder that mostly holds someone else, how many wait for review.
"""
import itertools
import random

import numpy as np

from .services import decide_group

CONFIRMED = ("new_group", "auto_matched")
PERCENTILES = (1, 5, 25, 50, 75, 95, 99)


def describe(values):
    """Count, mean, standard deviation, min, max and a few percentiles of a list of similarities."""
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return {"count": 0}
    result = {"count": int(values.size), "mean": float(values.mean()), "std": float(values.std()), "min": float(values.min()), "max": float(values.max())}
    result.update({f"p{p}": float(np.percentile(values, p)) for p in PERCENTILES})
    return result


def pair_similarities(vectors, labels, max_impostor_pairs=200_000, rng=None):
    """``(same_person, different_people)`` cosine similarities between photographs.

    Every same-person pair is used; different-person pairs are every one when there are fewer than ``max_impostor_pairs`` and a random sample of that
    many otherwise. ``vectors`` are unit vectors, one per photograph; ``labels`` the person of each.
    """
    rng = rng or random.Random(0)
    vectors = np.asarray(vectors, dtype=np.float32)
    labels = list(labels)
    by_person = {}
    for index, label in enumerate(labels):
        by_person.setdefault(label, []).append(index)
    same = []
    for indexes in by_person.values():
        for first, second in itertools.combinations(indexes, 2):
            same.append(float(np.dot(vectors[first], vectors[second])))
    total = len(labels) * (len(labels) - 1) // 2 - len(same)
    different = []
    if total <= max_impostor_pairs:
        for first, second in itertools.combinations(range(len(labels)), 2):
            if labels[first] != labels[second]:
                different.append(float(np.dot(vectors[first], vectors[second])))
    else:
        while len(different) < max_impostor_pairs:
            first, second = rng.randrange(len(labels)), rng.randrange(len(labels))
            if labels[first] != labels[second]:
                different.append(float(np.dot(vectors[first], vectors[second])))
    return np.asarray(same), np.asarray(different)


def operating_points(same, different, thresholds):
    """For each threshold: the share of same-person pairs at or above it (``tar``) and of different-person pairs at or above it (``far``)."""
    same, different = np.asarray(same), np.asarray(different)
    return [
        {"threshold": round(float(t), 4), "tar": float((same >= t).mean()) if same.size else None, "far": float((different >= t).mean()) if different.size else None}
        for t in thresholds
    ]


def threshold_at_far(different, far):
    """The smallest threshold at which at most ``far`` of the different-person pairs get through."""
    different = np.asarray(different)
    return float(np.quantile(different, 1.0 - far)) if different.size else None


def _unit(vector):
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def simulate_grouping(vectors, limits):
    """Place ``vectors`` in order the way ``identities.services`` does. Returns ``(assignments, centroids)``.

    ``assignments[i]`` is ``(folder index, state)`` with state ``new_group``, ``auto_matched`` or ``needs_review`` (a review photograph points at the folder
    it resembles most and does not join its average face, exactly as in the service). ``centroids`` is each folder's average face at the end.
    """
    members, centroids, assignments = [], [], []
    for vector in vectors:
        scored = sorted(((float(np.dot(vector, centroid)), index) for index, centroid in enumerate(centroids)), reverse=True)
        decision, folder, _best, _second = decide_group(scored, limits)
        if decision == "new":
            members.append([vector])
            centroids.append(_unit(np.mean(members[-1], axis=0)))
            assignments.append((len(members) - 1, "new_group"))
        elif decision == "match":
            members[folder].append(vector)
            centroids[folder] = _unit(np.mean(members[folder], axis=0))
            assignments.append((folder, "auto_matched"))
        else:
            assignments.append((folder, "needs_review"))
    return assignments, centroids


def album_metrics(labels, assignments):
    """What one simulated album came to, for the people ``labels`` names (one per photograph, in the order they arrived)."""
    confirmed = [(i, folder) for i, (folder, state) in enumerate(assignments) if state in CONFIRMED]
    folder_people = {}
    for i, folder in confirmed:
        folder_people.setdefault(folder, []).append(labels[i])
    majority = {folder: max(set(people), key=people.count) for folder, people in folder_people.items()}
    people = sorted(set(labels))
    folders_of = {person: {folder for i, folder in confirmed if labels[i] == person} for person in people}
    wrong = sum(1 for i, folder in confirmed if majority[folder] != labels[i])
    reviews = [(i, folder) for i, (folder, state) in enumerate(assignments) if state == "needs_review"]
    good_suggestion = sum(1 for i, folder in reviews if folder in majority and majority[folder] == labels[i])
    same_folder = [(a, b) for (a, fa), (b, fb) in itertools.combinations(confirmed, 2) if fa == fb]
    same_person = [(a, b) for (a, _fa), (b, _fb) in itertools.combinations(confirmed, 2) if labels[a] == labels[b]]
    true_pairs = sum(1 for a, b in same_folder if labels[a] == labels[b])
    return {
        "photos": len(labels),
        "people": len(people),
        "folders": len(folder_people),
        "folders_per_person": float(np.mean([max(1, len(folders_of[person])) for person in people])),
        "wrong_confirmed_rate": wrong / len(confirmed) if confirmed else 0.0,
        "review_rate": len(reviews) / len(labels),
        "review_suggestion_correct_rate": good_suggestion / len(reviews) if reviews else None,
        "pair_precision": true_pairs / len(same_folder) if same_folder else 1.0,
        "pair_recall": true_pairs / len(same_person) if same_person else 1.0,
    }


def sample_album(by_person, people, per_person, rng):
    """``(vectors, labels)`` of a random album: ``people`` people with ``per_person`` photographs each, in random order of arrival."""
    chosen = rng.sample(sorted(by_person), people)
    photos = []
    for person in chosen:
        indexes = by_person[person]
        photos.extend((index, person) for index in rng.sample(indexes, min(per_person, len(indexes))))
    rng.shuffle(photos)
    return [index for index, _person in photos], [person for _index, person in photos]


def evaluate_candidates(vectors, labels, candidates, margin, *, people=6, per_person=(6, 14), albums=60, seed=0):
    """Run the album simulation for each ``(new_group, match)`` pair; one row of mean metrics per pair, in the order given.

    Every candidate sees the same albums, so rows are comparable. ``per_person`` is the range of photographs of one person in an album.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    by_person = {}
    for index, label in enumerate(labels):
        by_person.setdefault(label, []).append(index)
    eligible = {person: indexes for person, indexes in by_person.items() if len(indexes) >= per_person[0]}
    if len(eligible) < people:
        raise ValueError(f"Only {len(eligible)} people have at least {per_person[0]} usable photographs; an album needs {people}.")
    rng = random.Random(seed)
    trials = []
    for _ in range(albums):
        size = rng.randint(*per_person)
        indexes, album_labels = sample_album(eligible, people, size, rng)
        trials.append(([vectors[i] for i in indexes], album_labels))
    rows = []
    for new_group, match in candidates:
        limits = {"new_group": new_group, "match": match, "margin": margin}
        results = []
        for album_vectors, album_labels in trials:
            assignments, _centroids = simulate_grouping(album_vectors, limits)
            results.append(album_metrics(album_labels, assignments))
        row = {"new_group": new_group, "match": match, "margin": margin, "albums": len(results)}
        for key in ("folders_per_person", "wrong_confirmed_rate", "review_rate", "pair_precision", "pair_recall"):
            row[key] = float(np.mean([result[key] for result in results]))
        suggestions = [result["review_suggestion_correct_rate"] for result in results if result["review_suggestion_correct_rate"] is not None]
        row["review_suggestion_correct_rate"] = float(np.mean(suggestions)) if suggestions else None
        rows.append(row)
    return rows


def recommend_limits(same, different, step=0.05, new_group_percentile=1.0, match_far=0.0001):
    """The pair of limits the pair distributions point to, on a grid of ``step``.

    * ``new_group`` - a face is "clearly not like" a folder when it is less alike than almost every photograph of the same person is to another: the
      ``new_group_percentile``-th percentile of same-person similarities, rounded **down** to the grid (so at most that share of true matches is lost).
    * ``match`` - a face joins a folder by itself only when different people almost never reach that similarity: the threshold that lets through at most
      ``match_far`` of different-person pairs, rounded **up** to the grid, and at least one step above ``new_group`` so uncertain photographs have a band
      to wait for review in.

    Returns the pair with the numbers it came from. The album simulation then shows what this pair does to a whole album.
    """
    same_floor = float(np.percentile(np.asarray(same), new_group_percentile))
    different_ceiling = threshold_at_far(different, match_far)
    # Within 2 % of a step of a grid line counts as on it (a percentile of 0.2998 is the 0.30 line, not the 0.25 one).
    new_group = float(np.floor(same_floor / step + 0.02) * step)
    match = float(max(np.ceil(different_ceiling / step - 0.02) * step, new_group + step))
    return {
        "new_group": round(new_group, 4), "match": round(match, 4), "same_person_percentile": new_group_percentile, "same_person_floor": same_floor,
        "match_far": match_far, "different_people_ceiling": different_ceiling,
    }


def recommend(rows, recommended_pair, max_wrong=0.01, max_review=0.30):
    """The row of the simulation for ``recommended_pair`` (``recommend_limits``), and whether it meets the safety limits.

    ``max_wrong`` is the most confirmed photographs that may sit in another person's folder, ``max_review`` the most that may wait for review. The row is
    returned either way; the text says which.
    """
    row = next((row for row in rows if (row["new_group"], row["match"]) == (recommended_pair["new_group"], recommended_pair["match"])), None)
    if row is None:
        raise ValueError("The recommended pair was not among the simulated candidates.")
    problems = []
    if row["wrong_confirmed_rate"] > max_wrong:
        problems.append(f"{row['wrong_confirmed_rate']:.1%} of confirmed photographs are in the wrong folder (limit {max_wrong:.1%})")
    if row["review_rate"] > max_review:
        problems.append(f"{row['review_rate']:.1%} of photographs wait for review (limit {max_review:.0%})")
    if problems:
        return row, "does NOT meet the safety limits: " + "; ".join(problems)
    return row, f"meets the safety limits ({row['wrong_confirmed_rate']:.1%} wrongly placed, {row['review_rate']:.1%} left for review)"


def folder_pair_similarities(vectors, labels, limits, albums=60, people=6, per_person=(6, 14), seed=0):
    """Average-face similarity between pairs of folders, split into folders of the same person and of different people.

    This is what the merge suggestion compares. Run with the limits chosen for grouping: it shows how alike two folders of one person are (they exist
    when grouping split a person) and how alike folders of different people get.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    by_person = {}
    for index, label in enumerate(labels):
        by_person.setdefault(label, []).append(index)
    eligible = {person: indexes for person, indexes in by_person.items() if len(indexes) >= per_person[0]}
    rng = random.Random(seed)
    same, different = [], []
    for _ in range(albums):
        indexes, album_labels = sample_album(eligible, people, rng.randint(*per_person), rng)
        album_vectors = [vectors[i] for i in indexes]
        assignments, centroids = simulate_grouping(album_vectors, limits)
        folder_people = {}
        for i, (folder, state) in enumerate(assignments):
            if state in CONFIRMED:
                folder_people.setdefault(folder, []).append(album_labels[i])
        majority = {folder: max(set(p), key=p.count) for folder, p in folder_people.items()}
        for first, second in itertools.combinations(sorted(majority), 2):
            similarity = float(np.dot(centroids[first], centroids[second]))
            (same if majority[first] == majority[second] else different).append(similarity)
    return np.asarray(same), np.asarray(different)

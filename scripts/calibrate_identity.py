"""Calibrate face-identity thresholds from labelled photographs: same-person and different-person similarity, under degradation.

    python scripts/calibrate_identity.py --dataset folder_of_people --out report_dir [--provider arcface|sface]

``folder_of_people`` holds one sub-folder per person with several photographs of them (``person_a/1.jpg, 2.jpg, ...``): the identity
labels are the folder names, so they must be real and the photographs obtained with consent or under a licence that allows this use
(for example a research face-verification set under its terms). Nothing here downloads or ships a dataset.

Per provider it embeds the largest face of every photograph, then reports

* **genuine** scores (two photographs of the same person) and **impostor** scores (two people), as distributions;
* for a chosen false-match rate (default 0.1 %, 1 %), the **threshold that gives it** and the true-match rate at that threshold:
  a threshold derived from impostor data, not picked;
* the same under each degradation (darkening, blur, noise, JPEG, downscale) applied to the *second* photograph, which is what the
  engine's identity gate sees (an enhanced or degraded face against the original);
* the equal-error rate; and the **people split**: the threshold is chosen on one half of the *people* and its false-match and
  true-match rates are reported on the other half, so a threshold is never judged on the identities it was fitted to.

Reading it: a similarity of 0.7 means nothing by itself; two embedding models do not share a scale (SFace and ArcFace are reported
separately and are not interchangeable); the number of people and the diversity of the set bound every claim made from it. With fewer
than about 100 people the report states that its false-match rate is not resolvable.
"""
import argparse
import itertools
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

DEGRADATIONS = ("none", "dark", "blur", "noise", "jpeg", "small")


def degrade(name, image, rng):
    import cv2
    import numpy as np

    if name == "none":
        return image
    if name == "dark":
        return np.clip(image.astype(np.float32) * 0.35 + rng.normal(0, 2.0, image.shape), 0, 255).astype(np.uint8)
    if name == "blur":
        return cv2.GaussianBlur(image, (0, 0), 2.5)
    if name == "noise":
        return np.clip(image.astype(np.float32) + rng.normal(0, 14.0, image.shape), 0, 255).astype(np.uint8)
    if name == "jpeg":
        return cv2.imdecode(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 25])[1], cv2.IMREAD_COLOR)
    if name == "small":
        height, width = image.shape[:2]
        return cv2.resize(cv2.resize(image, (max(32, width // 4), max(32, height // 4)), interpolation=cv2.INTER_AREA), (width, height),
                          interpolation=cv2.INTER_LINEAR)
    raise ValueError(name)


def threshold_at(impostors, false_match_rate):
    """The score above which only ``false_match_rate`` of impostor pairs fall (None when there are too few impostors to resolve it)."""
    import numpy as np

    if len(impostors) * false_match_rate < 5:
        return None
    return float(np.quantile(impostors, 1.0 - false_match_rate))


def rates(genuine, impostors, threshold):
    import numpy as np

    return {"true_match_rate": float(np.mean(np.asarray(genuine) >= threshold)) if len(genuine) else None,
            "false_match_rate": float(np.mean(np.asarray(impostors) >= threshold)) if len(impostors) else None}


def equal_error_rate(genuine, impostors):
    import numpy as np

    best = (1.0, None)
    for threshold in np.unique(np.concatenate([genuine, impostors])):
        frr = float(np.mean(np.asarray(genuine) < threshold))
        far = float(np.mean(np.asarray(impostors) >= threshold))
        if abs(frr - far) < best[0]:
            best = (abs(frr - far), (frr + far) / 2, float(threshold))
    return {"eer": best[1], "threshold": best[2]} if best[1] is not None else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--provider", choices=("arcface", "sface"), default="arcface")
    parser.add_argument("--false-match-rates", default="0.001,0.01")
    parser.add_argument("--max-edge", type=int, default=800)
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from identities.providers import ArcFaceOnnxProvider, SFaceOnnxProvider
    from validation.services import OpenCVYuNetFaceDetector

    provider = ArcFaceOnnxProvider() if arguments.provider == "arcface" else SFaceOnnxProvider()
    provider.healthcheck()
    detector = OpenCVYuNetFaceDetector()
    rng = np.random.default_rng(31)

    def embed(image):
        found = detector.detect(image)
        if not len(found.boxes):
            return None
        index = int(np.argmax([b[2] * b[3] for b in found.boxes]))
        x, y, w, h = found.boxes[index]
        try:
            return provider.embed(provider.prepare_face(image, {"x": x, "y": y, "width": w, "height": h}, found.landmarks[index]))
        except Exception:
            return None

    people = {}
    for folder in sorted(p for p in arguments.dataset.iterdir() if p.is_dir()):
        photos = []
        for path in sorted(q for q in folder.iterdir() if q.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}):
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                continue
            scale = arguments.max_edge / max(image.shape[:2])
            photos.append(cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image)
        if len(photos) >= 2:
            people[folder.name] = photos
    if len(people) < 2:
        sys.exit("Need at least two people with two photographs each.")

    names = sorted(people)
    reference = {name: [embed(p) for p in people[name]] for name in names}
    # the second photograph of every pair is the one degraded: that is what the engine's identity gate compares with the original
    probe_cache = {d: {name: [embed(degrade(d, p, rng)) for p in people[name]] for name in names} for d in DEGRADATIONS}
    scores = {d: {"genuine": [], "impostor": []} for d in DEGRADATIONS}
    for degradation in DEGRADATIONS:
        probe = probe_cache[degradation]
        for a in names:
            for i, j in itertools.permutations(range(len(people[a])), 2):
                if reference[a][i] is not None and probe[a][j] is not None:
                    scores[degradation]["genuine"].append(float(provider.similarity(reference[a][i], probe[a][j])))
        for a, b in itertools.permutations(names, 2):
            for i, j in itertools.product(range(len(people[a])), range(len(people[b]))):
                if reference[a][i] is not None and probe[b][j] is not None:
                    scores[degradation]["impostor"].append(float(provider.similarity(reference[a][i], probe[b][j])))
        print(degradation, len(scores[degradation]["genuine"]), "genuine", len(scores[degradation]["impostor"]), "impostor", flush=True)

    # people split: choose the threshold on half of the people, judge on the other half
    half = len(names) // 2
    fit, test = names[:half], names[half:]

    def split_scores(degradation, group):
        genuine, impostor = [], []
        for a in group:
            for i, j in itertools.permutations(range(len(people[a])), 2):
                if reference[a][i] is not None and probe_cache[degradation][a][j] is not None:
                    genuine.append(float(provider.similarity(reference[a][i], probe_cache[degradation][a][j])))
        for a, b in itertools.permutations(group, 2):
            for i, j in itertools.product(range(len(people[a])), range(len(people[b]))):
                if reference[a][i] is not None and probe_cache[degradation][b][j] is not None:
                    impostor.append(float(provider.similarity(reference[a][i], probe_cache[degradation][b][j])))
        return genuine, impostor

    report = {"provider": arguments.provider, "people": len(names), "photographs": sum(len(v) for v in people.values()),
              "false_match_rates": {}, "by_degradation": {}, "people_split": {}}
    for degradation in DEGRADATIONS:
        genuine, impostor = scores[degradation]["genuine"], scores[degradation]["impostor"]
        entry = {"genuine_n": len(genuine), "impostor_n": len(impostor),
                 "genuine_p05_p50_p95": [float(np.percentile(genuine, q)) for q in (5, 50, 95)] if genuine else None,
                 "impostor_p50_p95_p99": [float(np.percentile(impostor, q)) for q in (50, 95, 99)] if impostor else None,
                 "equal_error": equal_error_rate(np.array(genuine), np.array(impostor)) if genuine and impostor else None, "at_fmr": {}}
        for fmr in (float(x) for x in arguments.false_match_rates.split(",")):
            threshold = threshold_at(impostor, fmr)
            entry["at_fmr"][str(fmr)] = None if threshold is None else {"threshold": threshold, **rates(genuine, impostor, threshold)}
            fit_genuine, fit_impostor = split_scores(degradation, fit)
            fit_threshold = threshold_at(fit_impostor, fmr)
            test_genuine, test_impostor = split_scores(degradation, test)
            report["people_split"].setdefault(degradation, {})[str(fmr)] = None if fit_threshold is None else {
                "threshold_chosen_on": fit, "threshold": fit_threshold, "judged_on": test, **rates(test_genuine, test_impostor, fit_threshold)}
        report["by_degradation"][degradation] = entry
    note = [] if len(names) >= 100 else [f"Only {len(names)} people: false-match rates below about {1 / max(len(names), 1):.0%} are not resolvable."]
    report["notes"] = note + ["Similarity scales differ between providers; do not copy a threshold from one to the other.",
                              "A genuine pair here is two different photographs, so pose, light and age vary as in a real album."]
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "identity_calibration.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("provider", "people", "photographs", "notes")}, indent=2))


if __name__ == "__main__":
    main()

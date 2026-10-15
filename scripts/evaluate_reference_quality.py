"""Does enhancement recover the *original* face, and only that face?

Take a clean, undamaged face, damage it in a controlled way (darken, blur, shrink),
run it through the real (OpenCV and Pillow) pipeline, and check whether the final
result still looks like the *same person* as the untouched original -- and,
separately, whether two different people's faces are clearly less similar than that.
It shares ``identity_report``/``pick_sample``/``degrade`` with other evaluation
scripts through ``scripts/evaluation_common.py``.

Face embeddings are biometric data (Feature 41's classification). This tool computes
them only in memory, to produce a similarity **score**, and never writes an embedding
vector to disk, to ``results.jsonl`` or anywhere else -- only floats, strings and
image ids ever reach a file (a test asserts this).

    python scripts/evaluate_reference_quality.py --count 40
    python scripts/evaluate_reference_quality.py --images-dir Dataset/generated_photos/generated.photos --count 100

Output goes to ``.runtime/reference_eval/<time>/`` (``summary.md``, ``summary.json``,
``results.jsonl``, resumable with ``--resume --out <that folder>``). Read-only: it
never touches the database or the stored media.
"""
import argparse
import glob
import importlib.util
import json
import random
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("evaluation_common", Path(__file__).with_name("evaluation_common.py"))
common = importlib.util.module_from_spec(_spec)  # (name kept: the shared helpers used to live in evaluate_common.py)
_spec.loader.exec_module(common)

DEFAULT_IMAGE_DIRS = (
    ROOT / "faceimage" / "Academic_Dataset_by_Generated_Photos" / "generated.photos",
    ROOT / "Dataset" / "generated_photos" / "generated.photos",
    ROOT / "Dataset" / "generated_photos",
    ROOT / "People",
)


def resolve_images_dir(explicit):
    if explicit:
        return Path(explicit)
    for candidate in DEFAULT_IMAGE_DIRS:
        if candidate.is_dir() and (any(candidate.glob("*.jpg")) or any(candidate.glob("*.png"))):
            return candidate
    raise SystemExit(
        "No image folder found. Pass --images-dir, or place the dataset at one of: "
        + ", ".join(str(path) for path in DEFAULT_IMAGE_DIRS)
    )


def darken(image, factor, noise_sigma, seed):
    """A badly lit photo: multiply brightness down and add sensor-like grain."""
    import numpy as np

    rng = np.random.default_rng(seed)
    dim = image.astype(np.float32) * factor
    noisy = dim + rng.normal(0, noise_sigma, dim.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def _severity_functions():
    """Built lazily (needs cv2/numpy) so ``--help`` works without Django set up."""
    import cv2

    return {
        "clean": lambda image, seed: image,
        "dim": lambda image, seed: darken(image, 0.35, 4, seed),
        "dark": lambda image, seed: darken(image, 0.15, 8, seed),
        "dark_blur": lambda image, seed: cv2.GaussianBlur(darken(image, 0.15, 8, seed), (0, 0), 1.2),
        "low_res": lambda image, seed: common.degrade(image, shortest_edge=140, blur_sigma=1.0, jpeg_quality=45),
        "dark_low_res": lambda image, seed: common.degrade(
            darken(image, 0.30, 5, seed), shortest_edge=140, blur_sigma=1.0, jpeg_quality=45,
        ),
    }


def largest_face(image, detector):
    """``(box, landmarks)`` of the largest confident face, or ``None``."""
    detection = detector.detect(image)
    candidates = [
        (box[2] * box[3], box, landmarks)
        for box, landmarks in zip(detection.boxes, detection.landmarks)
        if landmarks and len(landmarks) == 5
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _area, box, landmarks = candidates[0]
    return box, landmarks


def embed_clean_faces(paths, detector, provider):
    """``{image name: embedding}`` for images with a confident face.

    Kept in memory by the caller only long enough to compute similarity *scores*;
    never written to disk (see the module docstring).
    """
    import cv2

    embeddings = {}
    for path in paths:
        image = cv2.imread(str(path))
        if image is None:
            continue
        found = largest_face(image, detector)
        if found is None:
            continue
        box, landmarks = found
        x, y, width, height = box
        try:
            aligned = provider.prepare_face(image, {"x": x, "y": y, "width": width, "height": height}, landmarks)
            embeddings[Path(path).name] = provider.embed(aligned)
        except Exception:  # noqa: BLE001 - one bad face must not stop the run
            continue
    return embeddings


def different_person_similarities(embeddings, provider, pair_count, seed):
    """Similarity scores for random pairs of *different* people's clean faces.

    Only the resulting floats are returned; the embeddings passed in are never
    themselves recorded anywhere by this tool.
    """
    names = sorted(embeddings)
    max_pairs = len(names) * (len(names) - 1) // 2
    rng = random.Random(seed)
    seen, similarities = set(), []
    attempts = 0
    while len(seen) < min(pair_count, max_pairs) and attempts < pair_count * 30:
        attempts += 1
        first, second = rng.sample(names, 2)
        key = tuple(sorted((first, second)))
        if key in seen:
            continue
        seen.add(key)
        similarities.append(float(provider.similarity(embeddings[first], embeddings[second])))
    return similarities


def measure_recovery(output_image, clean_embedding, detector, provider):
    """Similarity between the true clean original's face and the enhanced output's.

    This, not the pipeline's own internal identity guard, is the tool's headline
    number. The guard only ever compares the output to whatever was *fed into*
    ``enhance_bgr`` -- for a darkened photo, that is the dark image, not the clean
    original -- so on a badly degraded input it almost always reports "too dark to
    judge" even when the final, brightened output is perfectly judgeable against the
    real reference. This detects and embeds the output fresh and compares it to the
    clean embedding computed once per photo before degrading it.
    """
    found = largest_face(output_image, detector)
    if found is None:
        return {"status": "no_face_in_output", "similarity": None}
    box, landmarks = found
    x, y, width, height = box
    try:
        aligned = provider.prepare_face(output_image, {"x": x, "y": y, "width": width, "height": height}, landmarks)
        output_embedding = provider.embed(aligned)
    except Exception as error:  # noqa: BLE001 - one bad face must not stop the run
        return {"status": "embedding_failed", "similarity": None, "reason": str(error)[:120]}
    return {"status": "recovered", "similarity": float(provider.similarity(clean_embedding, output_embedding))}


def evaluate_severity(
    clean_image, clean_embedding, severity_name, degrade_fn, seed, detector, provider, enhance_bgr, analyze_image_quality,
):
    transformed = degrade_fn(clean_image, seed)
    started = time.perf_counter()
    try:
        out, level, parameters = enhance_bgr(transformed, "auto", allow_biometric_processing=True)
    except Exception as error:  # noqa: BLE001 - one bad photo must not stop the run
        return {"severity": severity_name, "error": f"{error.__class__.__name__}: {str(error)[:150]}"}
    quality = analyze_image_quality(out)
    return {
        "severity": severity_name,
        "seconds": round(time.perf_counter() - started, 3),
        "applied_level": str(level),
        "noise": quality["estimated_noise"],
        "luminance": quality["mean_luminance"],
        "identity": measure_recovery(out, clean_embedding, detector, provider),
        # Kept only as a diagnostic: whether the pipeline's own guard also fired, on
        # its own (less useful, input-not-original) comparison. Not the headline metric.
        "pipeline_guard": common.identity_report(parameters),
    }


def summarize_severity(records):
    good = [record for record in records if not record.get("error")]
    guards = [record["identity"] for record in good if record.get("identity")]
    similarities = [g["similarity"] for g in guards if isinstance(g.get("similarity"), (int, float))]
    summary = {"photos": len(records), "errors": len(records) - len(good)}
    if good:
        summary["mean_seconds"] = round(statistics.mean(r["seconds"] for r in good), 3)
        summary["mean_noise"] = round(statistics.mean(r["noise"] for r in good), 3)
    if guards:
        summary["identity_statuses"] = dict(Counter(g.get("status") for g in guards))
    if similarities:
        summary["same_person_mean_similarity"] = round(statistics.mean(similarities), 4)
        summary["same_person_min_similarity"] = round(min(similarities), 4)
    summary["same_person_judged"] = len(similarities)
    pipeline_guards = [record["pipeline_guard"] for record in good if record.get("pipeline_guard")]
    if pipeline_guards:
        summary["pipeline_guard_statuses"] = dict(Counter(g.get("status") for g in pipeline_guards))
    return summary


def markdown_report(severity_summaries, different_person, meta, threshold):
    lines = [
        f"# Reference-quality evaluation ({meta['run']})", "",
        f"Photos: {meta['photos']} (seed {meta['seed']}). "
        f"Faces usable for the different-person baseline: {meta['embedded_faces']}. Identity threshold: {threshold}.",
        "",
        "| Severity | OK/errors | Mean s | Mean noise | Identity judged | Same-person mean sim | Same-person min sim |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, summary in severity_summaries.items():
        if "mean_seconds" not in summary:
            lines.append(f"| {name} | 0/{summary['errors']} | | | | | |")
            continue
        lines.append(
            f"| {name} | {summary['photos'] - summary['errors']}/{summary['errors']} | {summary['mean_seconds']} | "
            f"{summary['mean_noise']} | {summary['same_person_judged']} | "
            f"{summary.get('same_person_mean_similarity', '-')} | {summary.get('same_person_min_similarity', '-')} |"
        )
    lines += ["", "## Different-person baseline (should sit well below the same-person numbers)", ""]
    if different_person:
        lowest_same_person = min(
            (s["same_person_min_similarity"] for s in severity_summaries.values() if "same_person_min_similarity" in s),
            default=None,
        )
        margin = (lowest_same_person - max(different_person)) if lowest_same_person is not None else None
        lines.append(
            f"Pairs compared: {len(different_person)}. Mean similarity: {round(statistics.mean(different_person), 4)}. "
            f"Highest similarity seen: {round(max(different_person), 4)}."
        )
        if margin is not None:
            verdict = "the threshold has room to spare" if margin > 0 else "**NO SAFETY MARGIN -- investigate**"
            lines.append(f"\nSafety margin (lowest same-person similarity minus highest different-person similarity): "
                         f"{round(margin, 4)} -- {verdict}.")
    else:
        lines.append("Not enough clean faces with a confident detection to build the baseline.")
    return "\n".join(lines) + "\n"


def setup_django():
    sys.path.insert(0, str(ROOT))
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images-dir", type=Path, default=None)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--severities", default="clean,dim,dark,dark_blur,low_res,dark_low_res")
    parser.add_argument("--pair-count", type=int, default=150, help="Different-person pairs to sample.")
    parser.add_argument("--montage", type=int, default=0, help="Save this many before/after photos as a picture (0 = none).")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    images_dir = resolve_images_dir(args.images_dir)
    paths = sorted(glob.glob(str(images_dir / "*.jpg"))) + sorted(glob.glob(str(images_dir / "*.png")))
    if not paths:
        print(f"No images found in {images_dir}.")
        return 2
    sample = common.pick_sample(paths, args.count, args.seed)
    severities = _severity_functions()
    unknown = [name for name in args.severities.split(",") if name.strip() and name.strip() not in severities]
    if unknown:
        print(f"Unknown severity/severities: {', '.join(unknown)}. Choose from: {', '.join(severities)}.")
        return 2
    chosen = [name.strip() for name in args.severities.split(",") if name.strip()]

    run = args.out or (ROOT / ".runtime" / "reference_eval" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True, exist_ok=True)
    results_path = run / "results.jsonl"

    setup_django()
    import cv2
    from django.conf import settings

    from enhancement.services import analyze_image_quality, enhance_bgr
    from identities.providers import get_identity_provider
    from validation.services import get_face_detector

    detector = get_face_detector()
    provider = get_identity_provider()
    provider.healthcheck()

    print(f"Building clean-face reference embeddings from {len(sample)} photos...")
    # Kept in memory for the length of this run to serve as the "true original" every
    # degraded/enhanced photo is compared against; never written to results.jsonl,
    # summary.json/md, or anywhere else -- only the resulting similarity floats are.
    embeddings = embed_clean_faces(sample, detector, provider)
    different_person = different_person_similarities(embeddings, provider, args.pair_count, args.seed)
    usable = [path for path in sample if Path(path).name in embeddings]
    print(
        f"  {len(embeddings)} of {len(sample)} photos had a confident clean face "
        f"({len(sample) - len(embeddings)} dropped); {len(different_person)} different-person pairs."
    )

    done = set()
    records = {name: [] for name in chosen}
    if args.resume and results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["severity"] in records:
                records[record["severity"]].append(record)
                done.add((record["severity"], record["photo"]))

    montage_rows = {}
    with results_path.open("a", encoding="utf-8") as sink:
        for index, path in enumerate(usable):
            name = Path(path).name
            image = cv2.imread(path)
            if image is None:
                continue
            for severity in chosen:
                if (severity, name) in done:
                    continue
                record = evaluate_severity(
                    image, embeddings[name], severity, severities[severity], args.seed + index,
                    detector, provider, enhance_bgr, analyze_image_quality,
                )
                record["photo"] = name
                sink.write(json.dumps(record) + "\n")
                sink.flush()
                records[severity].append(record)
                status = record.get("error") or f"sim {record['identity']['similarity'] if record.get('identity') else None}"
                print(f"  [{severity}] {index + 1}/{len(usable)} {name}: {status}")
                if args.montage and len(montage_rows) < args.montage and severity == chosen[-1]:
                    montage_rows[name] = image
    del embeddings  # still never persisted; discarded once every comparison above is done

    severity_summaries = {name: summarize_severity(items) for name, items in records.items()}
    meta = {
        # "photos" (== embedded_faces) is how many were actually evaluated: usable is
        # filtered to exactly the photos that had a confident clean-face embedding.
        "run": run.name, "photos": len(usable), "sampled": len(sample), "seed": args.seed,
        "images_dir": str(images_dir), "embedded_faces": len(usable),
    }
    threshold = float(settings.IDENTITY_GUARD_MIN_SIMILARITY)
    payload = {
        "meta": meta, "severities": severity_summaries,
        "different_person": {
            "pairs": len(different_person),
            "mean_similarity": round(statistics.mean(different_person), 4) if different_person else None,
            "max_similarity": round(max(different_person), 4) if different_person else None,
        },
    }
    (run / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report = markdown_report(severity_summaries, different_person, meta, threshold)
    (run / "summary.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Saved to {run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

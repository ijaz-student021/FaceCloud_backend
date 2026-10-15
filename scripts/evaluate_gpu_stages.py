"""Compare enhancement stage configurations on the same fixed photos.

Built for the GPU session, but it runs anywhere (slowly on a CPU). Each *configuration*
switches some stages on, runs the real ``enhance_bgr`` pipeline over the same sample, and
records time, noise, brightness, colour, detail, which stages actually ran (or why they
fell back) and, for face photos, the identity guard's similarity. It never touches the
database or the stored media.

    python scripts/evaluate_gpu_stages.py --configs baseline,purevision,hd --count 60
    python scripts/evaluate_gpu_stages.py --configs baseline,gfpgan --faces-dir People --face-count 20

Output goes to ``.runtime/gpu_eval/<run name>/``: ``results.jsonl`` (one line per photo and
configuration, so a stopped run resumes with ``--resume``), ``summary.json``, ``summary.md``
and a side-by-side ``montage.jpg`` of the first few photos.
"""
import argparse
import glob
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name -> settings to switch on for that configuration. ``baseline`` is the current default.
CONFIGS = {
    "baseline": {},
    "purevision": {"PUREVISION_ENABLED": True},
    "purescale": {"PURESCALE_ENABLED": True},
    "hd": {"HD_RESTORATION_ENABLED": True},
    "hd_deblur": {"HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True},
    "gfpgan": {"GFPGAN_ENABLED": True, "GFPGAN_QUEUE_ENABLED": False},
    "combo": {"PUREVISION_ENABLED": True, "HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True,
              "GFPGAN_ENABLED": True, "GFPGAN_QUEUE_ENABLED": False},
    "no_colour_gain": {"ENHANCEMENT_DARK_COLOUR_GAIN_MAX": 1.0},
}
# The result of each stage is recorded from these keys of the pipeline's parameters.
STAGE_KEYS = ("purevision", "purescale", "hd_restoration", "face_restoration", "restoration", "colorization", "zero_dce")
VISIBLE_NOISE = 5.0


def parse_configs(text):
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in CONFIGS]
    if unknown:
        raise ValueError(f"Unknown configuration(s): {', '.join(unknown)}. Choose from: {', '.join(CONFIGS)}.")
    if not names:
        raise ValueError("Choose at least one configuration.")
    return names


def pick_sample(paths, count, seed):
    """A reproducible sample: the same seed always gives the same photos."""
    ordered = sorted(paths)
    return random.Random(seed).sample(ordered, min(count, len(ordered)))


def export_sample(photos, faces, destination):
    """Copy the sampled photos into ``destination/images`` and ``destination/faces``.

    Lets the sample travel to another machine without copying the whole dataset;
    run there with ``--images-dir destination/images --faces-dir destination/faces``.
    """
    import shutil

    destination = Path(destination)
    for folder, paths in (("images", photos), ("faces", faces)):
        if paths:
            (destination / folder).mkdir(parents=True, exist_ok=True)
        for path in paths:
            shutil.copy2(path, destination / folder / Path(path).name)
    return len(photos), len(faces)


def stage_report(parameters):
    """``{stage: {"applied": bool, "fallback": str}}`` from a pipeline parameters dict."""
    report = {}
    for key in STAGE_KEYS:
        info = parameters.get(key)
        if isinstance(info, dict) and ("applied" in info or "fallback_reason" in info):
            report[key] = {"applied": bool(info.get("applied")), "fallback": str(info.get("fallback_reason") or "")[:120]}
    return report


def identity_report(parameters):
    guard = parameters.get("identity_guard")
    if not isinstance(guard, dict):
        return None
    report = {key: guard.get(key) for key in ("status", "similarity", "initial_similarity", "faces_compared", "threshold")}
    report["reason"] = str(guard.get("fallback_reason") or "")[:120]
    return report


def degrade(image, shortest_edge=200, blur_sigma=1.5, jpeg_quality=35):
    """A small, blurred, compressed copy: the kind of face GFPGAN is meant for.

    Face restoration only runs on photos flagged as blurry or low resolution, and
    ordinary portraits are not, so evaluating it needs inputs like this one.
    """
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    scale = min(1.0, shortest_edge / float(min(height, width)))
    small = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    blurred = cv2.GaussianBlur(small, (0, 0), blur_sigma)
    ok, encoded = cv2.imencode(".jpg", blurred, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return cv2.imdecode(np.frombuffer(encoded.tobytes(), dtype=np.uint8), cv2.IMREAD_COLOR) if ok else blurred


def summarize(records):
    """Aggregate one configuration's per-photo records into comparable numbers."""
    good = [r for r in records if not r.get("error")]
    summary = {"photos": len(records), "errors": len(records) - len(good)}
    if not good:
        return summary

    def mean(key):
        values = [r[key] for r in good if r.get(key) is not None]
        return round(statistics.mean(values), 3) if values else None

    summary.update({
        "mean_seconds": mean("seconds"),
        "median_seconds": round(statistics.median(r["seconds"] for r in good), 3),
        "max_seconds": round(max(r["seconds"] for r in good), 3),
        "mean_noise": mean("noise"),
        "share_visible_noise": round(sum(r["noise"] >= VISIBLE_NOISE for r in good) / len(good), 3),
        "mean_luminance": mean("luminance"),
        "mean_colourfulness": mean("colourfulness"),
        "mean_detail": mean("detail"),
    })
    applied, fallbacks = {}, {}
    for record in good:
        for stage, info in record.get("stages", {}).items():
            applied[stage] = applied.get(stage, 0) + int(info["applied"])
            if info["fallback"]:
                fallbacks.setdefault(stage, {}).setdefault(info["fallback"], 0)
                fallbacks[stage][info["fallback"]] += 1
    summary["stages_applied"] = applied
    summary["fallbacks"] = fallbacks
    guards = [r["identity"] for r in good if r.get("identity")]
    if guards:
        statuses = {}
        for guard in guards:
            statuses[guard.get("status")] = statuses.get(guard.get("status"), 0) + 1
        similarities = [g["similarity"] for g in guards if isinstance(g.get("similarity"), (int, float))]
        reasons = {}
        for guard in guards:
            if guard.get("reason"):
                reasons[guard["reason"]] = reasons.get(guard["reason"], 0) + 1
        summary["identity_guard"] = {
            "statuses": statuses,
            "reasons": reasons,
            "mean_similarity": round(statistics.mean(similarities), 4) if similarities else None,
            "min_similarity": round(min(similarities), 4) if similarities else None,
        }
    return summary


def compare_to_baseline(summaries, baseline="baseline"):
    """Change in each metric against the baseline configuration (negative noise is better)."""
    base = summaries.get(baseline)
    if not base or base.get("mean_noise") is None:
        return {}
    deltas = {}
    for name, summary in summaries.items():
        if name == baseline or summary.get("mean_noise") is None:
            continue
        deltas[name] = {
            "noise": round(summary["mean_noise"] - base["mean_noise"], 3),
            "visible_noise_share": round(summary["share_visible_noise"] - base["share_visible_noise"], 3),
            "colourfulness": round(summary["mean_colourfulness"] - base["mean_colourfulness"], 3),
            "detail": round(summary["mean_detail"] - base["mean_detail"], 3),
            "seconds": round(summary["mean_seconds"] - base["mean_seconds"], 3),
        }
    return deltas


def markdown_report(summaries, deltas, meta):
    lines = [f"# GPU stage comparison ({meta['run']})", "",
             f"Photos: {meta['photos']} (seed {meta['seed']}), face photos: {meta['face_photos']}. "
             f"Device: {meta['device']}.", "",
             "| Config | OK/errors | Mean s | Max s | Noise | Visible noise | Colour | Detail | Stages applied |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, s in summaries.items():
        if s.get("mean_seconds") is None:
            lines.append(f"| {name} | 0/{s['errors']} | | | | | | | |")
            continue
        applied = ", ".join(f"{k} {v}" for k, v in s["stages_applied"].items() if v) or "none"
        lines.append(f"| {name} | {s['photos'] - s['errors']}/{s['errors']} | {s['mean_seconds']} | {s['max_seconds']} | "
                     f"{s['mean_noise']} | {round(s['share_visible_noise'] * 100)}% | {s['mean_colourfulness']} | "
                     f"{s['mean_detail']} | {applied} |")
    if deltas:
        lines += ["", "## Change against baseline (noise, visible-noise share and seconds: lower is better)", "",
                  "| Config | Noise | Visible noise | Colour | Detail | Seconds |", "|---|---|---|---|---|---|"]
        for name, d in deltas.items():
            lines.append(f"| {name} | {d['noise']:+} | {d['visible_noise_share'] * 100:+.0f} pts | {d['colourfulness']:+} | "
                         f"{d['detail']:+} | {d['seconds']:+} |")
    fallbacks = {n: s["fallbacks"] for n, s in summaries.items() if s.get("fallbacks")}
    if fallbacks:
        lines += ["", "## Stages that fell back (and why)", ""]
        for name, stages in fallbacks.items():
            for stage, reasons in stages.items():
                for reason, count in reasons.items():
                    lines.append(f"- **{name}** / {stage}: {reason} ({count})")
    guards = {n: s["identity_guard"] for n, s in summaries.items() if s.get("identity_guard")}
    if guards:
        lines += ["", "## Identity guard on the face photos", ""]
        for name, g in guards.items():
            lines.append(f"- **{name}**: {g['statuses']}, mean similarity {g['mean_similarity']}, lowest {g['min_similarity']}")
            for reason, count in g.get("reasons", {}).items():
                lines.append(f"  - {reason} ({count})")
    return "\n".join(lines) + "\n"


def setup_django():
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def measure(image, out, parameters, seconds):
    import cv2
    import numpy as np

    from enhancement.services import analyze_image_quality

    quality = analyze_image_quality(out)
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB).astype(np.float32)
    return {
        "seconds": round(seconds, 3),
        "noise": quality["estimated_noise"],
        "luminance": quality["mean_luminance"],
        "colourfulness": round(float(np.mean(np.abs(lab[:, :, 1] - 128) + np.abs(lab[:, :, 2] - 128))), 3),
        "detail": round(float(cv2.Laplacian(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()), 2),
        "out_shape": list(out.shape[:2]), "in_shape": list(image.shape[:2]),
        "stages": stage_report(parameters), "identity": identity_report(parameters),
    }


def describe_device():
    try:
        import torch

        return f"{torch.cuda.get_device_name(0)} (CUDA)" if torch.cuda.is_available() else "CPU (no CUDA)"
    except Exception:  # noqa: BLE001
        return "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--configs", default="baseline", help=f"Comma list from: {', '.join(CONFIGS)}.")
    parser.add_argument("--images-dir", type=Path, default=ROOT / "Dataset" / "image")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--faces-dir", type=Path, default=None, help="Folder of face photos to test the identity guard.")
    parser.add_argument("--face-count", type=int, default=20)
    parser.add_argument("--degrade-faces", action="store_true",
                        help="Shrink, blur and compress the face photos first, so face restoration is actually requested.")
    parser.add_argument("--degrade-edge", type=int, default=200,
                        help="Shortest edge in pixels after degrading (larger keeps faces big enough for the identity guard).")
    parser.add_argument("--export-sample", type=Path, default=None,
                        help="Only copy the sampled photos into this folder (to take them to another machine), then stop.")
    parser.add_argument("--montage", type=int, default=4, help="Photos in the side-by-side image (0 = none).")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--resume", action="store_true", help="Skip photo/configuration pairs already in results.jsonl.")
    args = parser.parse_args(argv)

    names = parse_configs(args.configs)
    photos = pick_sample(glob.glob(str(args.images_dir / "*.png")) + glob.glob(str(args.images_dir / "*.jpg")), args.count, args.seed)
    faces = []
    if args.faces_dir:
        faces = pick_sample(glob.glob(str(args.faces_dir / "*.jpg")) + glob.glob(str(args.faces_dir / "*.png")), args.face_count, args.seed)
    if not photos and not faces:
        print("No photos found. Check --images-dir / --faces-dir.")
        return 2
    if args.export_sample:
        photo_count, face_count = export_sample(photos, faces, args.export_sample)
        print(f"Copied {photo_count} photo(s) and {face_count} face photo(s) to {args.export_sample}")
        return 0
    run = args.out or (ROOT / ".runtime" / "gpu_eval" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True, exist_ok=True)
    results_path = run / "results.jsonl"

    setup_django()
    import cv2
    import numpy as np
    from django.conf import settings

    from enhancement.services import enhance_bgr

    done = set()
    records = {name: [] for name in names}
    if args.resume and results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["config"] in records:
                records[record["config"]].append(record)
                done.add((record["config"], record["photo"]))

    items = [(path, False) for path in photos] + [(path, True) for path in faces]
    print(f"{len(items)} photo(s) x {len(names)} configuration(s) -> {run}")
    montage_rows = {}
    with results_path.open("a", encoding="utf-8") as sink:
        for name in names:
            original = {key: getattr(settings, key) for key in CONFIGS[name] if hasattr(settings, key)}
            for key, value in CONFIGS[name].items():
                setattr(settings, key, value)
            try:
                for index, (path, is_face) in enumerate(items, 1):
                    tag = f"{('faces-degraded/' if args.degrade_faces else 'faces/') if is_face else ''}{Path(path).name}"
                    if (name, tag) in done:
                        continue
                    image = cv2.imread(path)
                    if image is not None and is_face and args.degrade_faces:
                        image = degrade(image, shortest_edge=args.degrade_edge)
                    record = {"config": name, "photo": tag, "face_photo": is_face}
                    if image is None:
                        record["error"] = "unreadable image"
                    else:
                        try:
                            started = time.perf_counter()
                            out, _level, parameters = enhance_bgr(image, "auto")
                            record.update(measure(image, out, parameters, time.perf_counter() - started))
                            if args.montage and len(montage_rows.setdefault(tag, {})) < len(names) + 1 and len(montage_rows) <= args.montage:
                                montage_rows[tag].setdefault("original", image)
                                montage_rows[tag][name] = out
                        except Exception as error:  # noqa: BLE001 - one bad photo must not stop the run
                            record["error"] = f"{error.__class__.__name__}: {str(error)[:150]}"
                    sink.write(json.dumps(record) + "\n")
                    sink.flush()
                    records[name].append(record)
                    print(f"  [{name}] {index}/{len(items)} {tag}: "
                          f"{record.get('error') or str(record['seconds']) + 's, noise ' + str(record['noise'])}")
            finally:
                for key, value in original.items():
                    setattr(settings, key, value)

    summaries = {name: summarize(items_) for name, items_ in records.items()}
    deltas = compare_to_baseline(summaries)
    meta = {"run": run.name, "photos": len(photos), "seed": args.seed, "face_photos": len(faces), "device": describe_device()}
    (run / "summary.json").write_text(json.dumps({"meta": meta, "summaries": summaries, "vs_baseline": deltas}, indent=2), encoding="utf-8")
    (run / "summary.md").write_text(markdown_report(summaries, deltas, meta), encoding="utf-8")
    if args.montage and montage_rows:
        rows = []
        for tag, versions in list(montage_rows.items())[: args.montage]:
            columns = [versions["original"]] + [versions[n] for n in names if n in versions]
            rows.append(np.hstack([cv2.resize(c, (420, 280)) for c in columns]))
        width = max(row.shape[1] for row in rows)
        rows = [np.pad(row, ((0, 4), (0, width - row.shape[1]), (0, 0)), constant_values=255) for row in rows]
        cv2.imwrite(str(run / "montage.jpg"), np.vstack(rows))
    print("\n" + (run / "summary.md").read_text(encoding="utf-8"))
    print(f"Saved to {run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

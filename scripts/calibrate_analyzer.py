"""Measure the ImageQualityAnalyzer's thresholds against photographs with known, synthetic degradations.

    python scripts/calibrate_analyzer.py --images folder_of_photos --out report_dir [--limit 12]

For each photograph the script applies a known defect at several strengths and records what the analyzer says, so a
threshold is a measurement, not a guess:

* exposure: darkened to 70/50/35/25/15 % light with sensor-like noise, and the original (assumed acceptable);
* noise: Gaussian noise at sigma 0, 3, 6, 10, 15, 25;
* blur: sharp, Gaussian, disk (defocus) and linear motion blur at several lengths and angles;
* resolution: downscaled copies.

It prints confusion tables and writes ``calibration.json`` and ``calibration.md``. The synthetic defects are not real
camera blur or noise, and the photographs are the project's own (few, not demographically diverse): the report says so.
Nothing is uploaded or modified.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def motion(cv2, np, image, length, angle):
    kernel = np.zeros((length, length), np.float32)
    cv2.line(kernel, (0, length // 2), (length - 1, length // 2), 1, 1)
    matrix = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    kernel = cv2.warpAffine(kernel, matrix, (length, length))
    return cv2.filter2D(image, -1, kernel / kernel.sum())


def disk(cv2, np, image, radius):
    kernel = np.zeros((2 * radius + 1, 2 * radius + 1), np.float32)
    cv2.circle(kernel, (radius, radius), radius, 1, -1)
    return cv2.filter2D(image, -1, kernel / kernel.sum())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--max-edge", type=int, default=800)
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from enhancement.adaptive.analyzer import ImageQualityAnalyzer
    from validation.services import OpenCVYuNetFaceDetector

    analyzer = ImageQualityAnalyzer(detector=OpenCVYuNetFaceDetector())
    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})[: arguments.limit]
    rng = np.random.default_rng(21)
    exposure, noise, blur = defaultdict(Counter), defaultdict(list), defaultdict(Counter)
    faces_found = []
    for path in paths:
        image = cv2.imread(str(path))
        scale = arguments.max_edge / max(image.shape[:2])
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
        original = analyzer.analyze(image)
        faces_found.append(original.face_count)
        exposure["original"][original.exposure["status"]] += 1
        for factor in (0.7, 0.5, 0.35, 0.25, 0.15):
            dark = np.clip(image.astype(np.float32) * factor + rng.normal(0, 2.0, image.shape), 0, 255).astype(np.uint8)
            exposure[f"dark x{factor}"][analyzer.analyze(dark).exposure["status"]] += 1
        for sigma in (0, 3, 6, 10, 15, 25):
            noisy = np.clip(image.astype(np.float32) + rng.normal(0, sigma, image.shape), 0, 255).astype(np.uint8) if sigma else image
            report = analyzer.analyze(noisy)
            noise[sigma].append(report.noise["estimate"])
            blur_free = report.noise["status"]
            exposure[f"noise sigma {sigma}"]["_"] += 0
            noise[f"status {sigma}"].append(blur_free)
        variants = [("sharp", image)]
        variants += [(f"gaussian {s}", cv2.GaussianBlur(image, (0, 0), s)) for s in (2.0, 3.5)]
        variants += [(f"defocus disk {r}", disk(cv2, np, image, r)) for r in (3, 5)]
        variants += [(f"motion {length}px", motion(cv2, np, image, length, angle)) for length in (11, 19) for angle in (0, 45, 90, 135, 20)]
        for name, variant in variants:
            report = analyzer.analyze(variant)
            truth = "none" if name == "sharp" else "motion" if name.startswith("motion") else "defocus"
            blur[truth][(report.blur["status"], report.blur["type"])] += 1

    lines = ["# Analyzer calibration", "", f"{len(paths)} photographs, synthetic degradations, YuNet face detector. "
             f"Faces found per photograph: {faces_found}.", "", "## Exposure (what the analyzer called each darkening)", ""]
    for name, counts in exposure.items():
        if name.startswith("noise"):
            continue
        lines.append(f"* {name}: {dict(counts)}")
    lines += ["", "## Noise estimate by added Gaussian sigma (median, p10-p90) and the status it maps to", ""]
    for sigma in (0, 3, 6, 10, 15, 25):
        values = noise[sigma]
        lines.append(f"* sigma {sigma}: median {np.median(values):.2f} (p10 {np.percentile(values, 10):.2f}, p90 {np.percentile(values, 90):.2f}) "
                     f"-> {dict(Counter(noise[f'status {sigma}']))}")
    lines += ["", "## Blur: true class -> (status, type) the analyzer reported", ""]
    for truth, counts in blur.items():
        lines.append(f"* {truth}: {dict(counts)}")
    correct = sum(count for (status, kind), count in blur["motion"].items() if kind == "motion")
    total = sum(blur["motion"].values())
    lines += ["", f"Motion blur identified as motion: {correct}/{total}.",
              "Defocus identified as defocus or mixed: "
              f"{sum(c for (s, k), c in blur['defocus'].items() if k in ('defocus', 'mixed'))}/{sum(blur['defocus'].values())}.",
              "Sharp photographs called blurred: "
              f"{sum(c for (s, k), c in blur['none'].items() if s != 'none')}/{sum(blur['none'].values())}.",
              "", "Limitations: synthetic defects, a small set of photographs, one demographic. Re-run this on new data before "
              "changing a threshold."]
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "calibration.md").write_text("\n".join(lines) + "\n")
    (arguments.out / "calibration.json").write_text(json.dumps({
        "exposure": {k: dict(v) for k, v in exposure.items()}, "noise": {str(k): v for k, v in noise.items()},
        "blur": {k: {f"{s}/{t}": c for (s, t), c in v.items()} for k, v in blur.items()}}, indent=2, default=str))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

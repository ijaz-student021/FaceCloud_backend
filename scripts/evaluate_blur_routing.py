"""Measure whether the router sends the right photographs to the motion-deblurring model: false positives and false negatives.

    python scripts/evaluate_blur_routing.py --images folder_of_photos --out report_dir [--run-model]

The photographs are split by position in the sorted file list: the first ``--dev`` are the *development* set (the set
``calibrate_analyzer.py`` used, so a threshold may be examined on it) and the rest are the *held-out* set, which nothing was
ever tuned on; **the held-out table is the number to quote**. The degradations here are deliberately not the calibration ones:
other blur lengths (7-35 px instead of 11 and 19), random angles, camera-shake paths, blur on the subject only, other defocus
radii and Gaussian sigmas, blur combined with noise, JPEG recompression, and naturally soft (down- then up-scaled) photographs.

For each degraded copy the analyzer reports the blur type and the router says whether it would run the deblurring model. Truth:

* ``sharp`` (including noise, JPEG and soft variants) and ``defocus``: the model should NOT run (a run is a false positive);
* ``motion``, ``shake``, ``subject_motion``, ``mixed``: the model SHOULD run, except the mildest (``motion_7``, which may either run
  or not and is reported separately) (not running is a false negative).

With ``--run-model`` the real NAFNet-GoPro also runs on every copy, and PSNR against the clean photograph before and after
is reported per category, so "should run" is checked against what the model actually gains and not only against how the
degradation was made.

Honest limits: synthetic degradations on a handful of photographs of one phone and mostly one person; real camera shake and subject
motion are messier. A low false-positive rate here is necessary, not sufficient.
"""
import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def line_kernel(cv2, np, length, angle):
    kernel = np.zeros((length, length), np.float32)
    cv2.line(kernel, (0, length // 2), (length - 1, length // 2), 1, 1)
    matrix = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    kernel = cv2.warpAffine(kernel, matrix, (length, length))
    return kernel / kernel.sum()


def shake_kernel(cv2, np, rng, size):
    """A random-walk path (camera shake), thickened a little, normalised."""
    kernel = np.zeros((size, size), np.float32)
    point = np.array([size / 2, size / 2])
    direction = rng.uniform(0, 2 * np.pi)
    for _ in range(size * 2):
        direction += rng.normal(0, 0.35)
        point = np.clip(point + 0.5 * np.array([np.cos(direction), np.sin(direction)]), 1, size - 2)
        kernel[int(point[1]), int(point[0])] += 1
    kernel = cv2.GaussianBlur(kernel, (0, 0), 0.6)
    return kernel / kernel.sum()


def disk_kernel(cv2, np, radius):
    kernel = np.zeros((2 * radius + 1, 2 * radius + 1), np.float32)
    cv2.circle(kernel, (radius, radius), radius, 1, -1)
    return kernel / kernel.sum()


def variants(cv2, np, image, rng):
    """``[(name, truth, image)]`` for one photograph. truth: sharp | defocus | motion | mild_motion."""
    height, width = image.shape[:2]
    out = [("sharp", "sharp", image)]

    def noisy(picture, sigma):
        return np.clip(picture.astype(np.float32) + rng.normal(0, sigma, picture.shape), 0, 255).astype(np.uint8)

    def jpeg(picture, quality):
        return cv2.imdecode(cv2.imencode(".jpg", picture, [cv2.IMWRITE_JPEG_QUALITY, quality])[1], cv2.IMREAD_COLOR)

    out += [("sharp_noise4", "sharp", noisy(image, 4)), ("sharp_jpeg70", "sharp", jpeg(image, 70)),
            ("soft_resampled", "sharp", cv2.resize(cv2.resize(image, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA), (width, height),
                                                   interpolation=cv2.INTER_LINEAR))]
    for length in (7, 15, 27, 35):
        picture = cv2.filter2D(image, -1, line_kernel(cv2, np, length, float(rng.uniform(0, 180))))
        out.append((f"motion_{length}", "mild_motion" if length <= 7 else "motion", picture))
    for number in range(2):
        out.append((f"shake_{number}", "motion", cv2.filter2D(image, -1, shake_kernel(cv2, np, rng, int(rng.integers(17, 31))))))
    for length in (15, 25):
        blurred = cv2.filter2D(image, -1, line_kernel(cv2, np, length, float(rng.uniform(0, 180))))
        mask = np.zeros((height, width), np.float32)
        cv2.rectangle(mask, (int(width * 0.25), int(height * 0.15)), (int(width * 0.75), int(height * 0.85)), 1.0, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), 12)[..., None]
        out.append((f"subject_motion_{length}", "motion", np.clip(image * (1 - mask) + blurred * mask, 0, 255).astype(np.uint8)))
    for radius in (2, 4, 7, 9):
        out.append((f"defocus_disk_{radius}", "defocus", cv2.filter2D(image, -1, disk_kernel(cv2, np, radius))))
    for sigma in (1.2, 2.8, 5.0):
        out.append((f"defocus_gauss_{sigma}", "defocus", cv2.GaussianBlur(image, (0, 0), sigma)))
    mixed = cv2.filter2D(cv2.filter2D(image, -1, line_kernel(cv2, np, 15, float(rng.uniform(0, 180)))), -1, disk_kernel(cv2, np, 3))
    out.append(("mixed_motion15_disk3", "motion", mixed))
    motion_noisy = cv2.filter2D(image, -1, line_kernel(cv2, np, 15, float(rng.uniform(0, 180))))
    out += [("motion15_noise6", "motion", noisy(motion_noisy, 6)), ("motion15_noise12", "motion", noisy(motion_noisy, 12)),
            ("defocus4_noise6", "defocus", noisy(cv2.filter2D(image, -1, disk_kernel(cv2, np, 4)), 6))]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--dev", type=int, default=12, help="How many of the first photographs are the development set.")
    parser.add_argument("--max-edge", type=int, default=640)
    parser.add_argument("--run-model", action="store_true")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.test.utils import override_settings

    from enhancement.adaptive import operations as ops
    from enhancement.adaptive.analyzer import ImageQualityAnalyzer
    from enhancement.adaptive.router import AdaptiveEnhancementRouter
    from enhancement.adaptive.service import EngineConfig
    from validation.services import OpenCVYuNetFaceDetector

    analyzer = ImageQualityAnalyzer(detector=OpenCVYuNetFaceDetector())
    router = AdaptiveEnhancementRouter(EngineConfig())
    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    rng = np.random.default_rng(404)
    rows = []
    with override_settings(NAFNET_DEBLUR_ENABLED=True):
        for index, path in enumerate(paths):
            split = "development" if index < arguments.dev else "held_out"
            clean = cv2.imread(str(path), cv2.IMREAD_COLOR)
            scale = arguments.max_edge / max(clean.shape[:2])
            clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else clean
            for name, truth, picture in variants(cv2, np, clean, rng):
                report = analyzer.analyze(picture)
                step, reason = router._consider(ops.DEBLUR, report)
                row = {"photo": index, "split": split, "variant": name, "truth": truth, "runs_deblur": step is not None,
                       "blur": report.blur, "skip_reason": reason}
                if arguments.run_model:
                    from enhancement.hd_restoration import deblur_with_nafnet

                    output, _ = deblur_with_nafnet(picture)
                    row["psnr_before"], row["psnr_after"] = float(cv2.PSNR(clean, picture)), float(cv2.PSNR(clean, output))
                rows.append(row)
            print(index, split, flush=True)
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "blur_routing.json").write_text(json.dumps(rows, indent=2, default=str))

    lines = ["# Blur routing: false positives and false negatives", "",
             "`runs deblur` is the router's decision. Wrong decision = runs on sharp/defocus (false positive) or does not run on motion "
             "(false negative). `mild_motion` (7 px) is neutral.", ""]
    for split in ("development", "held_out"):
        mine = [r for r in rows if r["split"] == split]
        if not mine:
            continue
        lines += [f"## {split.replace('_', ' ')} set ({len({r['photo'] for r in mine})} photographs, {len(mine)} degraded copies)", "",
                  "| Variant | n | runs deblur | analyzer says (status/type) |", "|---|---|---|---|"]
        by_variant = defaultdict(list)
        for r in mine:
            by_variant[r["variant"].rstrip("_0123456789.") if r["variant"].startswith(("shake", "subject")) else r["variant"]].append(r)
        for name, group in by_variant.items():
            seen = defaultdict(int)
            for r in group:
                seen[f"{r['blur']['status']}/{r['blur']['type']}"] += 1
            lines.append(f"| {name} | {len(group)} | {sum(r['runs_deblur'] for r in group)}/{len(group)} | {dict(seen)} |")
        negatives = [r for r in mine if r["truth"] in ("sharp", "defocus")]
        positives = [r for r in mine if r["truth"] == "motion"]
        false_positive = sum(r["runs_deblur"] for r in negatives)
        false_negative = sum(not r["runs_deblur"] for r in positives)
        sharp_fp = sum(r["runs_deblur"] for r in mine if r["truth"] == "sharp")
        defocus_fp = sum(r["runs_deblur"] for r in mine if r["truth"] == "defocus")
        lines += ["", f"* False positives (ran on sharp or defocus): **{false_positive}/{len(negatives)}** "
                      f"(sharp {sharp_fp}/{sum(r['truth'] == 'sharp' for r in mine)}, defocus {defocus_fp}/{sum(r['truth'] == 'defocus' for r in mine)}).",
                  f"* False negatives (did not run on motion): **{false_negative}/{len(positives)}**.", ""]
        if arguments.run_model:
            lines += ["With the real NAFNet-GoPro run on every copy (PSNR against the clean photograph, mean over copies; the `sharp` row "
                      "starts from identical images, so its PSNR before is not meaningful, the loss count is):", "",
                      "| Truth | n | PSNR before | PSNR after | gain | copies where it lost quality |", "|---|---|---|---|---|---|"]
            for truth in ("sharp", "defocus", "mild_motion", "motion"):
                group = [r for r in mine if r["truth"] == truth and "psnr_after" in r]
                if group:
                    before, after = statistics.mean(r["psnr_before"] for r in group), statistics.mean(r["psnr_after"] for r in group)
                    lines.append(f"| {truth} | {len(group)} | {before:.2f} | {after:.2f} | {after - before:+.2f} | "
                                 f"{sum(r['psnr_after'] < r['psnr_before'] for r in group)}/{len(group)} |")
            lines.append("")
    (arguments.out / "blur_routing.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

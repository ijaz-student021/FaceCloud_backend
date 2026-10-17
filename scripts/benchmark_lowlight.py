"""Benchmark low-light enhancement on CPU, against the clean photograph each dark copy came from.

    python scripts/benchmark_lowlight.py --images my_clean_photos --out report_dir [--limit 12] [--levels 0.4,0.28,0.18]

Each clean photo is darkened to several brightness levels and given signal-dependent sensor noise (as a
real low-light capture has before 8-bit quantisation). Every configuration then enhances the dark copy and
is scored against the clean original:

* ``exposure error``: absolute difference of mean luminance from the clean photo (lower is better);
* ``structure SSIM``: SSIM of brightness-normalised luminance, so it measures detail and noise and not how
  bright the output is (higher is better);
* ``colour error``: mean distance of the LAB colour channels (lower is better);
* ``noise``: the pipeline's own estimate, and the time per photo on this CPU.

The configurations switch the rendering policy and the pre-filter per run in-process. Nothing is written but
the report (``summary.md``, ``results.json``) and a few side-by-side images. The photos are not modified,
uploaded or kept elsewhere.
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# name -> (policy, prefilter mode, level)
CONFIGURATIONS = {
    "lightweight, level high": ("lightweight_v1", "off", "high"),
    "lightweight + chroma pre-filter, high": ("lightweight_v1", "chroma", "high"),
    "lightweight + NLM pre-filter, high": ("lightweight_v1", "nlm", "high"),
    "balanced (Zero-DCE, CPU), medium": ("balanced_v1", "off", "medium"),
    "balanced (Zero-DCE, CPU), high": ("balanced_v1", "off", "high"),
    "automatic (default policy)": ("lightweight_v1", "off", "auto"),
}


def darken(clean, factor, seed):
    import numpy as np

    rng = np.random.default_rng(seed)
    dark = np.clip(clean.astype(np.float32) * factor, 0, 255)
    sigma = 1.5 + 0.35 * np.sqrt(dark)
    return np.clip(dark + rng.normal(0, 1, dark.shape) * sigma, 0, 255).astype(np.uint8)


def score(clean, output):
    import cv2
    import numpy as np

    from enhancement.services import _structural_similarity

    gray_clean = cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray_out = cv2.cvtColor(output, cv2.COLOR_BGR2GRAY).astype(np.float32)

    def normalised(gray):
        value = (gray - gray.mean()) / max(gray.std(), 1e-6)
        return np.clip(value * 40 + 128, 0, 255).astype(np.uint8)[..., None].repeat(3, axis=2)

    lab_clean = cv2.cvtColor(clean, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab_out = cv2.cvtColor(output, cv2.COLOR_BGR2LAB).astype(np.float32)
    return {
        "exposure_error": abs(float(gray_out.mean()) - float(gray_clean.mean())),
        "structure_ssim": _structural_similarity(normalised(gray_clean), normalised(gray_out)),
        "colour_error": float(np.mean(np.linalg.norm(lab_out[..., 1:] - lab_clean[..., 1:], axis=2))),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--levels", default="0.4,0.28,0.18", help="Brightness factors for the dark copies.")
    parser.add_argument("--max-edge", type=int, default=1280)
    parser.add_argument("--only", default="", help="Comma-separated configuration names (substrings).")
    parser.add_argument("--save", type=int, default=2, help="Side-by-side images to write.")
    arguments = parser.parse_args()

    import cv2
    import django

    django.setup()
    from django.test.utils import override_settings

    from enhancement.services import analyze_image_quality, enhance_bgr

    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)[: arguments.limit]
    if not paths:
        raise SystemExit(f"No images in {arguments.images}")
    factors = [float(value) for value in arguments.levels.split(",") if value.strip()]
    chosen = {name: spec for name, spec in CONFIGURATIONS.items()
              if not arguments.only or any(part.strip() in name for part in arguments.only.split(","))}
    arguments.out.mkdir(parents=True, exist_ok=True)

    rows, saved = [], 0
    for index, path in enumerate(paths):
        clean = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if clean is None:
            continue
        longest = max(clean.shape[:2])
        if longest > arguments.max_edge:
            scale = arguments.max_edge / longest
            clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        for factor in factors:
            dark = darken(clean, factor, seed=index * 100 + int(factor * 100))
            outputs = {}
            for name, (policy, prefilter, level) in chosen.items():
                with override_settings(ENHANCEMENT_RENDER_POLICY=policy, ENHANCEMENT_PREFILTER_MODE=prefilter):
                    started = time.perf_counter()
                    output, _applied, parameters = enhance_bgr(dark, level)
                    seconds = time.perf_counter() - started
                outputs[name] = output
                rows.append({
                    "photo": path.name, "brightness": factor, "configuration": name, "seconds": round(seconds, 3),
                    "noise": analyze_image_quality(output)["estimated_noise"],
                    "algorithm": parameters["algorithm"], "prefilter_applied": bool(parameters.get("prefilter", {}).get("applied")),
                    **{key: round(value, 4) for key, value in score(clean, output).items()},
                })
            if saved < arguments.save and factor == factors[len(factors) // 2]:
                import numpy as np

                tiles = [("clean", clean), ("dark input", dark)] + list(outputs.items())
                height = 360
                panel = np.hstack([
                    cv2.putText(cv2.resize(image, (round(image.shape[1] * height / image.shape[0]), height)),
                                label[:34], (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
                    for label, image in tiles
                ])
                cv2.imwrite(str(arguments.out / f"{path.stem}__x{factor}.jpg"), panel, [cv2.IMWRITE_JPEG_QUALITY, 88])
                saved += 1

    summary = {}
    for name in chosen:
        mine = [row for row in rows if row["configuration"] == name]
        summary[name] = {
            "runs": len(mine),
            "exposure_error": round(statistics.mean(row["exposure_error"] for row in mine), 2),
            "structure_ssim": round(statistics.mean(row["structure_ssim"] for row in mine), 4),
            "colour_error": round(statistics.mean(row["colour_error"] for row in mine), 2),
            "noise": round(statistics.mean(row["noise"] for row in mine), 2),
            "seconds": round(statistics.mean(row["seconds"] for row in mine), 2),
        }
    (arguments.out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    lines = [
        "# Low-light benchmark (CPU)", "",
        f"{len(paths)} photos x {len(factors)} darkness levels ({', '.join(map(str, factors))}); each dark copy has "
        "sensor-like noise; scored against the clean photo.", "",
        "| Configuration | Exposure error (lower) | Structure SSIM (higher) | Colour error (lower) | Noise | Seconds |",
        "|---|---|---|---|---|---|",
    ]
    for name, values in summary.items():
        lines.append(f"| {name} | {values['exposure_error']} | {values['structure_ssim']} | {values['colour_error']} | "
                     f"{values['noise']} | {values['seconds']} |")
    (arguments.out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

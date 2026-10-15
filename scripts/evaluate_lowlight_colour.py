"""Measure how natural the colours of a low-light lift are, against the clean photograph, for several colour strategies.

    python scripts/evaluate_lowlight_colour.py --images folder_of_photos --out report_dir [--max-edge 640]

Each clean photograph is darkened in two physically different ways (see ``darken``), the real Retinexformer runs once on it, and
the same raw network output is then turned into an image by each strategy:

* ``raw``                  the network output as it is (its own colours);
* ``chroma_p<x>``          the shipped approach: the network's lightness with the *source's* chroma scaled by (lightness gain)^x;
* ``chromaticity_linear``  the source's chromaticity (linear RGB colour ratios, smoothed) with the network's luminance.

For each it reports, against the clean photograph: chroma error (CIELAB a/b distance), chroma retention (output chroma / clean
chroma, below 1 is paler), hue shift in degrees, the same on skin pixels, the brightness left over (mean L), PSNR, SSIM and
LPIPS. Photographs are split by name into a **calibration half** (used to choose a setting) and a **held-out half** (reported
only, never used to choose), so a setting is not tuned on the photographs it is judged on.

Honest limits: the darkening is synthetic (a real night photograph has its own noise, white balance and tone curve); the 16
photographs come from one phone and mostly one person; numbers say which strategy keeps colour closer to the clean photograph,
not that any output looks right to a viewer.
"""
import argparse
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

LEVELS = ("gamma_x0.25", "linear_1/4", "linear_1/8", "linear_1/16")


def darken(clean, level, rng):
    """``gamma_x0.25``: the earlier benchmark (code values x0.25 plus faint noise). ``linear_1/k``: scene light divided by k in
    linear light, with shot (signal-dependent) plus read noise, then re-encoded and quantised: closer to what a sensor records."""
    import numpy as np

    from enhancement.adaptive.colour import from_linear, to_linear

    if level == "gamma_x0.25":
        return np.clip(clean.astype(np.float32) * 0.25 + rng.normal(0, 2.0, clean.shape), 0, 255).astype(np.uint8)
    k = float(level.split("/")[1])
    dark = to_linear(clean) / k
    sigma = np.sqrt(3e-4 * np.maximum(dark, 0) + (2.4e-3) ** 2)
    return from_linear(dark + rng.normal(0, 1, dark.shape).astype(np.float32) * sigma)


def lab(image):
    import cv2
    import numpy as np

    return cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)


def skin_mask(clean):
    import cv2

    ycrcb = cv2.cvtColor(clean, cv2.COLOR_BGR2YCrCb)
    return cv2.inRange(ycrcb, (0, 135, 85), (255, 170, 125)) > 0


def colour_metrics(clean, image):
    import numpy as np

    a, b = lab(clean), lab(image)
    chroma_a, chroma_b = np.hypot(a[..., 1], a[..., 2]), np.hypot(b[..., 1], b[..., 2])
    mid = (a[..., 0] > 15) & (a[..., 0] < 92)
    coloured = mid & (chroma_a > 12)
    hue = np.degrees(np.abs(np.arctan2(a[..., 2], a[..., 1]) - np.arctan2(b[..., 2], b[..., 1])))
    hue = np.minimum(hue, 360 - hue)
    delta_ab = np.hypot(a[..., 1] - b[..., 1], a[..., 2] - b[..., 2])
    skin = coloured & skin_mask(clean)

    def region(mask):
        if mask.sum() < 50:
            return {"chroma_error": None, "retention": None, "hue_shift": None}
        return {"chroma_error": float(delta_ab[mask].mean()), "retention": float(np.median(chroma_b[mask] / chroma_a[mask])),
                "hue_shift": float(hue[mask].mean())}

    return {"all": region(coloured), "skin": region(skin), "mean_L_clean": float(a[..., 0].mean()), "mean_L_image": float(b[..., 0].mean()),
            "clipped_percent": float(((image >= 250).any(axis=2)).mean() * 100)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-edge", type=int, default=640)
    parser.add_argument("--exponents", default="0.5,0.75,1.0")
    parser.add_argument("--levels", default=",".join(LEVELS))
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.conf import settings

    from enhancement import services
    from enhancement.adaptive.colour import chromaticity_linear, lift_colour_metrics
    from enhancement.adaptive.validator import reference_metrics
    from enhancement.lowlight import enhance_with_retinexformer

    exponents = [float(x) for x in arguments.exponents.split(",")]
    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    rng = np.random.default_rng(11)
    rows = []
    for index, path in enumerate(paths):
        split = "calibration" if index % 2 == 0 else "held_out"
        clean = cv2.imread(str(path), cv2.IMREAD_COLOR)
        scale = arguments.max_edge / max(clean.shape[:2])
        clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else clean
        for level in arguments.levels.split(","):
            dark = darken(clean, level, rng)
            raw, _meta = enhance_with_retinexformer(dark)
            protected = services._preserve_source_chroma(dark, raw)
            variants = {"dark_input": dark, "raw": raw}
            for exponent in exponents:
                variants[f"chroma_p{exponent}"], _ = services._amplify_source_chroma(
                    dark, protected, settings.ENHANCEMENT_DARK_COLOUR_GAIN_MAX, exponent=exponent)
            variants["chromaticity_linear"] = chromaticity_linear(dark, raw)
            for name, image in variants.items():
                rows.append({"photo": index, "split": split, "level": level, "variant": name, "colour": colour_metrics(clean, image),
                             "reference": reference_metrics(clean, image),
                             "lift": lift_colour_metrics(dark, image)})
            print(f"{index:2d} {split:11s} {level:12s} done", flush=True)

    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "lowlight_colour.json").write_text(json.dumps(rows, indent=2))

    def mean(values):
        values = [v for v in values if v is not None]
        return round(statistics.mean(values), 3) if values else None

    lines = ["# Low-light colour evaluation", "", f"{len(paths)} photographs x {len(arguments.levels.split(','))} darkening levels; "
             "synthetic darkening; scored against the clean photograph. Chroma error: lower is better. Retention: 1.0 means as colourful "
             "as the clean photograph, below 1 is paler. Hue shift in degrees.", ""]
    for split in ("calibration", "held_out"):
        lines += [f"## {split.replace('_', ' ')} photographs", "",
                  "| Level | Variant | Chroma error | Retention | Hue shift | Skin chroma error | Skin retention | PSNR | SSIM | LPIPS | Gate: hue shift vs input | Gate: saturation ratio vs input |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for level in arguments.levels.split(","):
            for name in ["dark_input", "raw"] + [f"chroma_p{e}" for e in exponents] + ["chromaticity_linear"]:
                mine = [r for r in rows if r["split"] == split and r["level"] == level and r["variant"] == name]
                lines.append(
                    f"| {level} | {name} | {mean([r['colour']['all']['chroma_error'] for r in mine])} | "
                    f"{mean([r['colour']['all']['retention'] for r in mine])} | {mean([r['colour']['all']['hue_shift'] for r in mine])} | "
                    f"{mean([r['colour']['skin']['chroma_error'] for r in mine])} | {mean([r['colour']['skin']['retention'] for r in mine])} | "
                    f"{mean([r['reference']['psnr'] for r in mine])} | {mean([r['reference']['ssim'] for r in mine])} | "
                    f"{mean([r['reference'].get('lpips') for r in mine])} | {mean([r['lift']['hue_shift'] for r in mine])} | "
                    f"{mean([r['lift']['saturation_ratio'] for r in mine])} |")
        lines.append("")
    (arguments.out / "lowlight_colour.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

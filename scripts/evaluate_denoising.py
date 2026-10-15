"""Compare denoising methods on three kinds of noise, against the clean photograph, including how much face texture survives.

    python scripts/evaluate_denoising.py --images folder_of_photos --out report_dir [--max-edge 640]

Noise cases (each applied to every clean photograph):

* ``gauss_<s>``        independent Gaussian noise (the textbook case; the least like a phone);
* ``phone_<k>``        signal-dependent shot noise plus read noise in linear light, then re-encoded and JPEG-compressed (quality 85):
                       closer to what a phone sensor and its encoder produce, and spatially a little correlated after compression;
* ``lifted_<k>``       a photograph darkened by ``k`` in linear light with sensor noise, then brightened by the real Retinexformer:
                       the grain a low-light lift actually leaves behind (every second photograph, to bound time).

Methods: ``none``, ``nafnet_sidd`` (the model at full strength), ``nafnet_sidd_65`` (65 % blended with its input), ``opencv_nlm``
(the engine's edge-aware non-local means). Reported per case and method: PSNR, SSIM, LPIPS against the clean photograph,
``face_texture`` (structure energy of the face region divided by the clean photograph's: 1.0 keeps the texture, well under 1.0 is
smoothing), and ``grain`` (the analyzer's flat-region grain level).

Honest limits: every noise here is synthetic, including ``phone_*``; no real smartphone noise pair was available, so nothing here
shows that a method works on real phone noise. SIDD (the dataset NAFNet-SIDD was trained on) is real smartphone noise, which is the
reason to expect the model to transfer, not evidence that it does on these photographs.
"""
import argparse
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def add_noise(case, clean, rng):
    import cv2
    import numpy as np

    from enhancement.adaptive.colour import from_linear, to_linear

    kind, level = case.split("_", 1)
    if kind == "gauss":
        return np.clip(clean.astype(np.float32) + rng.normal(0, float(level), clean.shape), 0, 255).astype(np.uint8)
    if kind == "phone":
        linear = to_linear(clean)
        sigma = np.sqrt(float(level) * np.maximum(linear, 0) + (0.004) ** 2)
        noisy = from_linear(linear + rng.normal(0, 1, linear.shape).astype(np.float32) * sigma)
        return cv2.imdecode(cv2.imencode(".jpg", noisy, [cv2.IMWRITE_JPEG_QUALITY, 85])[1], cv2.IMREAD_COLOR)
    raise ValueError(case)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-edge", type=int, default=640)
    parser.add_argument("--cases", default="gauss_6,gauss_12,gauss_20,phone_0.002,phone_0.008,lifted_8,lifted_16")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.test.utils import override_settings

    from enhancement import services
    from enhancement.adaptive.analyzer import flat_noise
    from enhancement.adaptive.colour import chromaticity_linear
    from enhancement.adaptive.validator import reference_metrics, structure_energy
    from enhancement.hd_restoration import denoise_with_nafnet
    from enhancement.lowlight import enhance_with_retinexformer
    from evaluate_lowlight_colour import darken
    from validation.services import OpenCVYuNetFaceDetector

    detector = OpenCVYuNetFaceDetector()
    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    rng = np.random.default_rng(17)
    rows = []

    def face_crop(image, box):
        x, y, w, h = box
        return image[max(0, y):y + h, max(0, x):x + w]

    for index, path in enumerate(paths):
        clean = cv2.imread(str(path), cv2.IMREAD_COLOR)
        scale = arguments.max_edge / max(clean.shape[:2])
        clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else clean
        found = detector.detect(clean)
        box = None
        if len(found.boxes):
            box = [int(v) for v in found.boxes[int(np.argmax([b[2] * b[3] for b in found.boxes]))]]
        clean_texture = structure_energy(face_crop(clean, box)) if box else None
        for case in arguments.cases.split(","):
            if case.startswith("lifted_"):
                if index % 2:
                    continue
                dark = darken(clean, f"linear_1/{case.split('_')[1]}", rng)
                with override_settings(RETINEXFORMER_ENABLED=True):
                    raw, _ = enhance_with_retinexformer(dark)
                noisy = chromaticity_linear(dark, raw)
            else:
                noisy = add_noise(case, clean, rng)
            with override_settings(NAFNET_DENOISE_ENABLED=True):
                nafnet, _meta = denoise_with_nafnet(noisy)
            outputs = {"none": noisy, "nafnet_sidd": nafnet, "nafnet_sidd_65": cv2.addWeighted(nafnet, 0.65, noisy, 0.35, 0),
                       "opencv_nlm": services._edge_aware_denoise(noisy, 10)}
            for method, image in outputs.items():
                reference = reference_metrics(clean, image)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                rows.append({"photo": index, "case": case, "method": method, "psnr": reference["psnr"], "ssim": reference["ssim"],
                             "lpips": reference.get("lpips"), "grain": flat_noise(gray),
                             "face_texture": (structure_energy(face_crop(image, box)) / clean_texture) if box and clean_texture else None})
            print(index, case, flush=True)

    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "denoising.json").write_text(json.dumps(rows, indent=2))

    def mean(values):
        values = [v for v in values if v is not None]
        return round(statistics.mean(values), 3) if values else None

    lines = ["# Denoising evaluation", "", "Scored against the clean photograph; synthetic noise only (see the script's docstring).", "",
             "| Case | Method | n | PSNR | SSIM | LPIPS | Face texture kept | Grain (flat-region level) |", "|---|---|---|---|---|---|---|---|"]
    for case in arguments.cases.split(","):
        for method in ("none", "nafnet_sidd", "nafnet_sidd_65", "opencv_nlm"):
            mine = [r for r in rows if r["case"] == case and r["method"] == method]
            if mine:
                lines.append(f"| {case} | {method} | {len(mine)} | {mean([r['psnr'] for r in mine])} | {mean([r['ssim'] for r in mine])} | "
                             f"{mean([r['lpips'] for r in mine])} | {mean([r['face_texture'] for r in mine])} | {mean([r['grain'] for r in mine])} |")
    (arguments.out / "denoising.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

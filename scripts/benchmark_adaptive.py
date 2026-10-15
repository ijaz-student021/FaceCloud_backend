"""Benchmark the complete adaptive pipeline against clean references, per kind of defect.

    python scripts/benchmark_adaptive.py --images folder_of_photos --out report_dir [--limit 4] [--categories dark,noisy,...]

Each photograph is the ground truth. For each category a known degradation is applied to it, the *whole* adaptive engine
(analysis, routing, candidates, validation, selection) runs on the degraded copy, and the result is compared with the clean
photograph:

* ``dark``: 25 % light plus sensor-like noise;  ``noisy``: Gaussian noise (sigma 18);  ``motion``: linear motion blur;
* ``defocus``: disk blur (the engine should report this as not recoverable, not pretend);  ``lowres``: 4x down and back up;
* ``face``: blur and JPEG damage on a face;  ``clean``: the untouched photograph (the engine should leave it alone).

For every run it records the outcome, the operations that ran and were rejected, the time, the peak resident memory, PSNR
and SSIM against the clean photograph before and after (and LPIPS when the optional ``lpips`` package and weights are
available), and the ArcFace / SFace similarity between the clean face and the result. Images are brought to the reference's
size before comparison and the report says when that was needed.

Honest limits: the degradations are synthetic; PSNR and SSIM reward smoothness and do not measure how a person judges a
photograph (a GAN upscaler scores lower than bicubic on PSNR while looking sharper); a handful of photographs is a
demonstration, not a proof. Nothing is uploaded; the photographs are not modified.
"""
import argparse
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

CATEGORIES = ("clean", "dark", "noisy", "motion", "defocus", "lowres", "face")


def degrade(category, clean, rng):
    import cv2
    import numpy as np

    if category == "clean":
        return clean.copy()
    if category == "dark":
        return np.clip(clean.astype(np.float32) * 0.25 + rng.normal(0, 2.0, clean.shape), 0, 255).astype(np.uint8)
    if category == "noisy":
        return np.clip(clean.astype(np.float32) + rng.normal(0, 18.0, clean.shape), 0, 255).astype(np.uint8)
    if category == "motion":
        kernel = np.zeros((19, 19), np.float32)
        kernel[9, :] = 1 / 19
        return cv2.filter2D(clean, -1, kernel)
    if category == "defocus":
        kernel = np.zeros((11, 11), np.float32)
        cv2.circle(kernel, (5, 5), 5, 1, -1)
        return cv2.filter2D(clean, -1, kernel / kernel.sum())
    if category == "lowres":
        height, width = clean.shape[:2]
        small = cv2.resize(clean, (width // 4, height // 4), interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (width, height), interpolation=cv2.INTER_CUBIC)
    if category == "face":
        blurred = cv2.GaussianBlur(clean, (0, 0), 2.2)
        return cv2.imdecode(cv2.imencode(".jpg", blurred, [cv2.IMWRITE_JPEG_QUALITY, 30])[1], cv2.IMREAD_COLOR)
    raise ValueError(category)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--max-edge", type=int, default=800)
    parser.add_argument("--categories", default=",".join(CATEGORIES))
    parser.add_argument("--skip", type=int, default=0, help="Skip this many photographs first.")
    parser.add_argument("--identity", choices=("sface", "arcface"), default="sface",
                        help="The identity model the engine's identity gate uses (the report measures both either way).")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.test.utils import override_settings

    from enhancement.adaptive.validator import reference_metrics
    from enhancement.services import enhance_bgr

    flags = dict(ENHANCEMENT_RENDER_POLICY="adaptive_v1", RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=False, NAFNET_DENOISE_ENABLED=True,
                 NAFNET_DEBLUR_ENABLED=True, HD_RESTORATION_ENABLED=True, CODEFORMER_ENABLED=True, GFPGAN_ENABLED=False,
                 ADAPTIVE_DEBUG_ARTIFACTS=False,
                 IDENTITY_PROVIDER="arcface_w600k_r50_v1" if arguments.identity == "arcface" else "sface_2021dec_v1")
    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    paths = paths[arguments.skip: arguments.skip + arguments.limit]
    categories = [c for c in arguments.categories.split(",") if c]
    arguments.out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(5)

    identity = {}
    try:
        from identities.providers import ArcFaceOnnxProvider, SFaceOnnxProvider
        from validation.services import OpenCVYuNetFaceDetector

        detector = OpenCVYuNetFaceDetector()
        for label, provider in (("sface", SFaceOnnxProvider()), ("arcface", ArcFaceOnnxProvider())):
            try:
                provider.healthcheck()
                identity[label] = (provider, detector)
            except Exception:
                pass
    except Exception:
        pass

    def face_similarity(clean, image):
        values = {}
        for label, (provider, detector) in identity.items():
            try:
                def embed(picture):
                    found = detector.detect(picture)
                    index = int(np.argmax([b[2] * b[3] for b in found.boxes]))
                    x, y, w, h = found.boxes[index]
                    return provider.embed(provider.prepare_face(picture, {"x": x, "y": y, "width": w, "height": h}, found.landmarks[index]))

                resized = cv2.resize(image, (clean.shape[1], clean.shape[0]), interpolation=cv2.INTER_AREA)
                values[label] = round(float(provider.similarity(embed(clean), embed(resized))), 4)
            except Exception:
                values[label] = None
        return values

    rows, saved = [], set()
    for path in paths:
        clean = cv2.imread(str(path), cv2.IMREAD_COLOR)
        scale = arguments.max_edge / max(clean.shape[:2])
        clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else clean
        for category in categories:
            degraded = degrade(category, clean, rng)
            started = time.perf_counter()
            with override_settings(**flags):
                try:
                    output, _level, parameters = enhance_bgr(degraded, "auto")
                    error = None
                except Exception as exc:  # reported, not hidden
                    output, parameters, error = degraded, {"adaptive": {}}, f"{type(exc).__name__}: {str(exc)[:120]}"
            seconds = round(time.perf_counter() - started, 1)
            adaptive = parameters.get("adaptive", {})
            before = reference_metrics(clean, degraded)
            after = reference_metrics(clean, output)
            rows.append({
                "photo": path.name, "category": category, "outcome": adaptive.get("outcome", "failed" if error else None),
                "applied": adaptive.get("operations_applied", []),
                "rejected": [s["operation"] for s in adaptive.get("stages", []) if s["status"] in ("rejected", "unavailable")],
                "skipped": [item["operation"] for item in adaptive.get("operations_skipped", [])],
                "seconds": seconds, "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024), 0),
                "identical_to_input": bool(np.array_equal(output, degraded)),
                "before": before, "after": after,
                "identity_before": face_similarity(clean, degraded), "identity_after": face_similarity(clean, output),
                "gate": {key: parameters.get("identity_verification", {}).get(key) for key in ("status", "similarity", "threshold")},
                "summary": adaptive.get("summary"), "error": error,
            })
            if category not in saved:
                saved.add(category)
                height = 360
                tiles = [cv2.resize(image, (int(image.shape[1] * height / image.shape[0]), height)) for image in (clean, degraded, output)]
                for tile, label in zip(tiles, ("clean", f"degraded ({category})", f"adaptive: {adaptive.get('outcome')}")):
                    cv2.putText(tile, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.imwrite(str(arguments.out / f"{category}.jpg"), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 88])
            print(f"{path.name[:8]} {category:8s} {rows[-1]['outcome']!s:20s} {seconds:6.1f}s applied={rows[-1]['applied']}", flush=True)

    lines = ["# Adaptive pipeline benchmark", "",
             f"{len(paths)} photographs x {len(categories)} categories, synthetic degradations, scored against the clean photograph.", "",
             "| Category | Runs | Outcomes | PSNR before -> after | SSIM before -> after | LPIPS after | Identical to input | Seconds (median) | ArcFace / SFace similarity after |",
             "|---|---|---|---|---|---|---|---|---|"]
    for category in categories:
        mine = [r for r in rows if r["category"] == category]
        if not mine:
            continue
        outcomes = {}
        for r in mine:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        mean = lambda values: round(statistics.mean(values), 3) if values else None  # noqa: E731
        lpips_after = [r["after"]["lpips"] for r in mine if r["after"].get("lpips") is not None]
        arc = [r["identity_after"].get("arcface") for r in mine if r["identity_after"].get("arcface") is not None]
        sface = [r["identity_after"].get("sface") for r in mine if r["identity_after"].get("sface") is not None]
        lines.append(
            f"| {category} | {len(mine)} | {outcomes} | {mean([r['before']['psnr'] for r in mine])} -> {mean([r['after']['psnr'] for r in mine])} | "
            f"{mean([r['before']['ssim'] for r in mine])} -> {mean([r['after']['ssim'] for r in mine])} | {mean(lpips_after)} | "
            f"{sum(r['identical_to_input'] for r in mine)}/{len(mine)} | {statistics.median([r['seconds'] for r in mine])} | {mean(arc)} / {mean(sface)} |")
    lines += ["", "Peak resident memory of the process: %s MB. Errors: %s." % (
        max((r["peak_rss_mb"] for r in rows), default=0), [r["error"] for r in rows if r["error"]] or "none"), "",
        "Reading it: `clean` should be identical to its input (nothing was changed that did not need to be); `defocus` should "
        "be preserved or partial, never claimed fixed; PSNR/SSIM are blunt (see the module docstring).", ""]
    (arguments.out / "summary.md").write_text("\n".join(lines))
    (arguments.out / "results.json").write_text(json.dumps(rows, indent=2, default=str))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

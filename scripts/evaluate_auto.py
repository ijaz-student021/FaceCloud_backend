"""Measure strength "Automatic" on six photographs with different problems, with the real models.

    python scripts/evaluate_auto.py --images folder --out report.md [--style natural]

One clean photograph is degraded six ways (dim, noisy, motion blur, defocus blur, a 240 px low-resolution copy, and the untouched clean
photograph), each is run through ``enhance_bgr`` with level ``auto`` (the adaptive engine, HD target, measured strengths), and for each the
plan is printed with the numbers before and after: face detail (Laplacian variance of the face at 128 px), the face's median luminance,
the noise level (1.0 is "moderate"), chroma drift against the input (mean CIELAB a/b difference at the input's size), the output resolution
and the seconds. Nothing is written but the report, and the photographs are never committed.

Honest limits: synthetic degradations of one photograph from one phone; no human judgement; one Apple-silicon device.
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_DEBUG", "True")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True)
    parser.add_argument("--index", type=int, default=0, help="Which photograph in the sorted folder to use.")
    parser.add_argument("--long-edge", type=int, default=1280)
    parser.add_argument("--style", default="natural")
    parser.add_argument("--out", default="")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.conf import settings

    from enhancement.adaptive.service import AdaptiveImageEnhancementService
    from enhancement.restoration import stage_status
    from enhancement.services import enhance_bgr

    settings.ENHANCEMENT_AUTO_ADAPTIVE = True
    analyzer = AdaptiveImageEnhancementService()._analyzer()
    path = sorted(p for p in Path(arguments.images).expanduser().iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))[arguments.index]
    clean = cv2.imread(str(path))
    scale = arguments.long_edge / max(clean.shape[:2])
    if scale < 1:
        clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    rng = np.random.default_rng(7)

    def disk(radius):
        kernel = np.zeros((2 * radius + 1, 2 * radius + 1), np.float32)
        cv2.circle(kernel, (radius, radius), radius, 1.0, -1)
        return kernel / kernel.sum()

    def line(length=25, angle=20.0):
        kernel = np.zeros((length, length), np.float32)
        cv2.line(kernel, (0, length // 2), (length - 1, length // 2), 1.0, 1)
        kernel = cv2.warpAffine(kernel, cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1), (length, length))
        return kernel / kernel.sum()

    low = 240.0 / min(clean.shape[:2])
    fixtures = {
        "dim": np.clip(clean.astype(np.float32) * 0.22, 0, 255).astype(np.uint8),
        "noisy": np.clip(clean.astype(np.float32) + rng.normal(0, 26, clean.shape), 0, 255).astype(np.uint8),
        "motion_blur": cv2.filter2D(clean, -1, line(), borderType=cv2.BORDER_REFLECT),
        "defocus_blur": cv2.filter2D(clean, -1, disk(6), borderType=cv2.BORDER_REFLECT),
        "low_res_240": cv2.resize(clean, (max(1, round(clean.shape[1] * low)), max(1, round(clean.shape[0] * low))), interpolation=cv2.INTER_AREA),
        "clean": clean,
    }

    def measure(image):
        report = analyzer.analyze(image)
        main_face = report.main_face
        return {
            "face_detail": None if main_face is None else main_face.detail,
            "face_median": None if main_face is None else main_face.luminance.get("p50"),
            "noise": report.noise.get("level"), "size": f"{image.shape[1]}x{image.shape[0]}", "report": report,
        }

    def chroma_drift(before, after):
        resized = cv2.resize(after, (before.shape[1], before.shape[0]), interpolation=cv2.INTER_AREA)
        a = cv2.cvtColor(before, cv2.COLOR_BGR2LAB).astype(np.float32)
        b = cv2.cvtColor(resized, cv2.COLOR_BGR2LAB).astype(np.float32)
        return round(float(np.mean(np.abs(a[:, :, 1:] - b[:, :, 1:]))), 2)

    lines = ["# Strength Automatic on six photographs with different problems", "",
             "Stages available: " + ", ".join(f"{k}={'yes' if v['available'] else 'NO'}" for k, v in stage_status().items()) + f"; style `{arguments.style}`.", "",
             "| fixture | face detail before -> after | face median luminance | noise level | chroma drift | size | seconds | plan |", "|---|---|---|---|---|---|---|---|"]
    notes = []
    for name, image in fixtures.items():
        before = measure(image)
        started = time.perf_counter()
        output, _level, parameters = enhance_bgr(image, style=arguments.style, allow_biometric_processing=True)
        seconds = time.perf_counter() - started
        after = measure(output)
        adaptive = parameters.get("adaptive") or {}
        plan = adaptive.get("plan", {}).get("operations", [])
        steps = "; ".join(f"{op['stage']} {op['status']}" + (f" ({op['engine']}, strength {op['strength'] if op['strength'] is not None else op['planned_strength']})" if op["engine"] else "") for op in plan) or "none"
        lines.append(f"| {name} | {before['face_detail']} -> {after['face_detail']} | {before['face_median']} -> {after['face_median']} | "
                     f"{before['noise']} -> {after['noise']} | {chroma_drift(image, output)} | {before['size']} -> {after['size']} | {seconds:.0f} | {steps} |")
        notes.append(f"* **{name}**: {adaptive.get('summary', parameters.get('auto_adaptive'))}")
        print(lines[-1], flush=True)
    text = "\n".join(lines + ["", "## Summaries as the API returns them", ""] + notes)
    print(text)
    if arguments.out:
        Path(arguments.out).write_text(text + "\n")


if __name__ == "__main__":
    main()

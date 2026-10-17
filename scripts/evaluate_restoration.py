"""Measure what the restoration chain (deblur, upscale to HD) does to a blurred or low-resolution face, with the real models.

    FACE_DETECTOR_PROVIDER=yunet_v1 YUNET_MODEL_SHA256=... python scripts/evaluate_restoration.py --images folder --count 4 --out report.md

For each photograph (resized to ``--long-edge`` px) three degraded copies are made from the *clean* photograph and run through
``enhance_bgr`` exactly as a user's upload would be (the default policy, restoration escalation on):

* ``mild_blur``: Gaussian blur sigma 2.5 over the whole photograph;
* ``strong_blur``: sigma 7 (the "radius 7" case in the product brief);
* ``low_res_240``: the photograph downscaled until its shorter edge is 240 px.

Reported per copy: what ran (``applied_stages``), the face detail (Laplacian variance of the main face resized to 128 px, the measure the
quality decision and the router use; a sharp face is about 119, below 25 is strongly blurred) before and after, the same on the *clean*
photograph for reference, the output size, the seconds, and the engine's outcome and summary. Numbers only: no image is written, and the
photographs are never committed.

Honest limits: synthetic degradations of a few photographs of one phone; no human judgement of the result; Gaussian defocus is not real
camera blur; one Apple-silicon device (MPS), so the seconds do not transfer to a server.
"""
import argparse
import os
import statistics
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
    parser.add_argument("--count", type=int, default=4, help="How many photographs with a detectable face to use.")
    parser.add_argument("--long-edge", type=int, default=1280)
    parser.add_argument("--out", default="")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from enhancement.adaptive.analyzer import ImageQualityAnalyzer
    from enhancement.adaptive.service import AdaptiveImageEnhancementService
    from enhancement.restoration import describe_stages, stage_status
    from enhancement.services import enhance_bgr

    analyzer = AdaptiveImageEnhancementService()._analyzer()
    status = stage_status()
    lines = ["# Restoration chain: measured on real photographs", "",
             "Stages available on the machine that ran this: " + ", ".join(f"{k}={'yes' if v['available'] else 'NO'}" for k, v in status.items()), ""]

    def face_detail(image):
        report = analyzer.analyze(image)
        main_face = report.main_face
        return (None if main_face is None else main_face.detail), report

    def degrade(clean, kind):
        if kind == "mild_blur":
            return cv2.GaussianBlur(clean, (0, 0), 2.5)
        if kind == "strong_blur":
            return cv2.GaussianBlur(clean, (0, 0), 7.0)
        scale = 240.0 / min(clean.shape[:2])
        return cv2.resize(clean, (max(1, round(clean.shape[1] * scale)), max(1, round(clean.shape[0] * scale))), interpolation=cv2.INTER_AREA)

    rows = []
    used = 0
    for path in sorted(Path(arguments.images).expanduser().iterdir()):
        if used >= arguments.count or path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        clean = cv2.imread(str(path))
        if clean is None:
            continue
        scale = arguments.long_edge / max(clean.shape[:2])
        if scale < 1:
            clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        clean_detail, clean_report = face_detail(clean)
        if clean_detail is None:
            continue
        used += 1
        for kind in ("mild_blur", "strong_blur", "low_res_240"):
            degraded = degrade(clean, kind)
            before, before_report = face_detail(degraded)
            started = time.perf_counter()
            output, _level, parameters = enhance_bgr(degraded, allow_biometric_processing=False)
            seconds = time.perf_counter() - started
            after, after_report = face_detail(output)
            stages = describe_stages(parameters)
            rows.append({
                "photo": f"p{used}", "case": kind, "clean": clean_detail, "before": before, "after": after,
                "size_in": f"{degraded.shape[1]}x{degraded.shape[0]}", "size_out": f"{output.shape[1]}x{output.shape[0]}",
                "stages": ", ".join(s["stage"] + (f" x{s['scale']}" if s.get("scale") else "") for s in stages["applied_stages"]) or "none",
                "why_not": stages["hd_unavailable_reason"] or "", "seconds": round(seconds, 1),
                "outcome": (parameters.get("adaptive") or {}).get("outcome", "staged"),
                "escalated": "restoration_escalation" in parameters, "frame_blur_in": before_report.blur["status"],
                "face_blur_in": before_report.face_blur.get("status"),
            })
            print(rows[-1], flush=True)

    lines += ["| photo | case | face detail clean | before | after | face blur seen | size in -> out | stages run | seconds | outcome |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['photo']} | {r['case']} | {r['clean']} | {r['before']} | {r['after']} | {r['face_blur_in']} | {r['size_in']} -> {r['size_out']} | {r['stages']} | {r['seconds']} | {r['outcome']} |")
    lines += ["", "## By case (medians)", "", "| case | n | face detail before | after | gain (after/before) | escalated | output larger |", "|---|---|---|---|---|---|---|"]
    for kind in ("mild_blur", "strong_blur", "low_res_240"):
        group = [r for r in rows if r["case"] == kind and r["before"] and r["after"]]
        if not group:
            continue
        gains = [r["after"] / r["before"] for r in group]
        lines.append(
            f"| {kind} | {len(group)} | {statistics.median(r['before'] for r in group):.1f} | {statistics.median(r['after'] for r in group):.1f} | "
            f"x{statistics.median(gains):.2f} | {sum(r['escalated'] for r in group)}/{len(group)} | {sum(r['size_out'] != r['size_in'] for r in group)}/{len(group)} |"
        )
    reasons = sorted({r["why_not"] for r in rows if r["why_not"]})
    if reasons:
        lines += ["", "## Restoration needed but not done", ""] + [f"* {reason}" for reason in reasons]
    text = "\n".join(lines)
    print(text)
    if arguments.out:
        Path(arguments.out).write_text(text + "\n")


if __name__ == "__main__":
    main()

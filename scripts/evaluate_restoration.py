"""Measure what the restoration chain does to a degraded face, with the real models, chain by chain.

    python scripts/evaluate_restoration.py --images folder --count 1 --out report.md [--cases a,b] [--chains x,y]

For each photograph (resized to ``--long-edge`` px) degraded copies are made from the *clean* photograph and run through ``enhance_bgr``
exactly as a user's upload would be (default policy, restoration escalation on, biometric processing on so face restoration may run):

* ``mild_blur``: Gaussian blur sigma 2.5; ``strong_blur``: sigma 7 (the "radius 7" case in the product brief);
* ``defocus``: a disk (lens) kernel of radius 6 px; ``motion``: a straight 25 px line at 20 degrees;
* ``low_res_240``: the photograph reduced until its shorter edge is 240 px.

Each case runs under each **chain** (which stages are switched on): ``nafnet`` (deblur only), ``nafnet+gfpgan``, ``nafnet+codeformer``,
``full`` (NAFNet, CodeFormer with GFPGAN as the fallback, Real-ESRGAN). Reported: what ran (``applied_stages``), the face detail (Laplacian
variance of the main face resized to 128 px; the measure the quality decision and the router use) before and after and on the clean
photograph, PSNR of the output against the clean photograph (both at the output's size), the output size, seconds, and the engine's outcome.
No image is written and the photographs are never committed.

Honest limits: synthetic degradations of a few photographs of one phone; PSNR and a face-detail number are not a human's judgement (a
generative face restorer can look sharper while scoring lower PSNR); no human evaluation; one Apple-silicon device (MPS), so the seconds
do not transfer to a server.
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

CHAINS = {
    "nafnet": dict(NAFNET_DEBLUR_ENABLED=True, GFPGAN_ENABLED=False, CODEFORMER_ENABLED=False, HD_RESTORATION_ENABLED=False),
    "nafnet+gfpgan": dict(NAFNET_DEBLUR_ENABLED=True, GFPGAN_ENABLED=True, CODEFORMER_ENABLED=False, HD_RESTORATION_ENABLED=False),
    "nafnet+codeformer": dict(NAFNET_DEBLUR_ENABLED=True, GFPGAN_ENABLED=False, CODEFORMER_ENABLED=True, HD_RESTORATION_ENABLED=False),
    "full": dict(NAFNET_DEBLUR_ENABLED=True, GFPGAN_ENABLED=True, CODEFORMER_ENABLED=True, HD_RESTORATION_ENABLED=True),
}
CASES = ("mild_blur", "strong_blur", "defocus", "motion", "low_res_240")


def degrade(cv2, np, clean, kind):
    if kind == "mild_blur":
        return cv2.GaussianBlur(clean, (0, 0), 2.5)
    if kind == "strong_blur":
        return cv2.GaussianBlur(clean, (0, 0), 7.0)
    if kind == "defocus":
        radius = 6
        kernel = np.zeros((2 * radius + 1, 2 * radius + 1), np.float32)
        cv2.circle(kernel, (radius, radius), radius, 1.0, -1)
        return cv2.filter2D(clean, -1, kernel / kernel.sum(), borderType=cv2.BORDER_REFLECT)
    if kind == "motion":
        length, angle = 25, 20.0
        kernel = np.zeros((length, length), np.float32)
        cv2.line(kernel, (0, length // 2), (length - 1, length // 2), 1.0, 1)
        matrix = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
        kernel = cv2.warpAffine(kernel, matrix, (length, length))
        return cv2.filter2D(clean, -1, kernel / kernel.sum(), borderType=cv2.BORDER_REFLECT)
    scale = 240.0 / min(clean.shape[:2])
    return cv2.resize(clean, (max(1, round(clean.shape[1] * scale)), max(1, round(clean.shape[0] * scale))), interpolation=cv2.INTER_AREA)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True)
    parser.add_argument("--count", type=int, default=2, help="How many photographs with a detectable face to use.")
    parser.add_argument("--long-edge", type=int, default=1280)
    parser.add_argument("--cases", default=",".join(CASES))
    parser.add_argument("--chains", default="nafnet,nafnet+gfpgan,full")
    parser.add_argument("--out", default="")
    arguments = parser.parse_args()
    cases = [c for c in arguments.cases.split(",") if c]
    chains = [c for c in arguments.chains.split(",") if c]

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.conf import settings

    from enhancement.adaptive.service import AdaptiveImageEnhancementService
    from enhancement.restoration import describe_stages, stage_status
    from enhancement.services import enhance_bgr

    analyzer = AdaptiveImageEnhancementService()._analyzer()
    lines = ["# Restoration chains on real photographs", "",
             "Stages available on the machine that ran this (all switched on): " + ", ".join(
                 f"{k}={'yes' if v['available'] else 'NO'}" for k, v in stage_status().items()), ""]

    def face_detail(image):
        report = analyzer.analyze(image)
        main_face = report.main_face
        return (None if main_face is None else main_face.detail), report

    def psnr_vs(clean, output):
        reference = cv2.resize(clean, (output.shape[1], output.shape[0]), interpolation=cv2.INTER_AREA if clean.shape[1] > output.shape[1] else cv2.INTER_CUBIC)
        return round(float(cv2.PSNR(reference, output)), 2)

    rows, used = [], 0
    for path in sorted(Path(arguments.images).expanduser().iterdir()):
        if used >= arguments.count or path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        clean = cv2.imread(str(path))
        if clean is None:
            continue
        scale = arguments.long_edge / max(clean.shape[:2])
        if scale < 1:
            clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        clean_detail, _ = face_detail(clean)
        if clean_detail is None:
            continue
        used += 1
        for kind in cases:
            degraded = degrade(cv2, np, clean, kind)
            before, before_report = face_detail(degraded)
            psnr_before = psnr_vs(clean, degraded) if degraded.shape == clean.shape else None
            for chain in chains:
                for name, value in CHAINS[chain].items():
                    setattr(settings, name, value)
                started = time.perf_counter()
                output, _level, parameters = enhance_bgr(degraded, allow_biometric_processing=True)
                seconds = time.perf_counter() - started
                after, _ = face_detail(output)
                stages = describe_stages(parameters)
                row = {
                    "photo": f"p{used}", "case": kind, "chain": chain, "clean": clean_detail, "before": before, "after": after,
                    "psnr_before": psnr_before, "psnr_after": psnr_vs(clean, output),
                    "size_in": f"{degraded.shape[1]}x{degraded.shape[0]}", "size_out": f"{output.shape[1]}x{output.shape[0]}",
                    "stages": ", ".join(s["stage"] + (f" x{s['scale']}" if s.get("scale") else "") + (f" ({s['engine']})" if s["stage"] == "face_restore" else "")
                                        for s in stages["applied_stages"]) or "none",
                    "why_not": stages["hd_unavailable_reason"] or "", "seconds": round(seconds, 1),
                    "outcome": (parameters.get("adaptive") or {}).get("outcome", "staged"),
                    "face_blur_in": before_report.face_blur.get("status"),
                }
                rows.append(row)
                print(row, flush=True)

    lines += ["| photo | case | chain | face detail clean / before / after | PSNR vs clean before -> after | size in -> out | stages run | seconds | outcome |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['photo']} | {r['case']} | {r['chain']} | {r['clean']} / {r['before']} / {r['after']} | {r['psnr_before']} -> {r['psnr_after']} "
                     f"| {r['size_in']} -> {r['size_out']} | {r['stages']} | {r['seconds']} | {r['outcome']} |")
    lines += ["", "## By case and chain (medians)", "", "| case | chain | n | face detail before | after | gain | PSNR after | stages run (first photo) | seconds |", "|---|---|---|---|---|---|---|---|---|"]
    for kind in cases:
        for chain in chains:
            group = [r for r in rows if r["case"] == kind and r["chain"] == chain and r["before"] and r["after"]]
            if not group:
                continue
            gains = [r["after"] / r["before"] for r in group]
            lines.append(
                f"| {kind} | {chain} | {len(group)} | {statistics.median(r['before'] for r in group):.1f} | {statistics.median(r['after'] for r in group):.1f} | "
                f"x{statistics.median(gains):.2f} | {statistics.median(r['psnr_after'] for r in group):.1f} | {group[0]['stages']} | {statistics.median(r['seconds'] for r in group):.0f} |"
            )
    reasons = sorted({r["why_not"] for r in rows if r["why_not"]})
    if reasons:
        lines += ["", "## Restoration needed but not done (in this evaluation)", ""] + [f"* {reason}" for reason in reasons]
    text = "\n".join(lines)
    print(text)
    if arguments.out:
        Path(arguments.out).write_text(text + "\n")


if __name__ == "__main__":
    main()

"""Evaluate the enhancement pipeline on the kinds of photo it must handle.

Each source photo is degraded in a known way (dark, noisy, blurred, compressed,
overexposed, grayscale, a group of faces, a transparent cut-out) and run through
the real ``enhance_bgr`` with this machine's settings. Because the clean photo is
known, the report can say whether the result moved *toward* it or away from it,
not just that some metric went up.

    python scripts/evaluate_pipeline_cases.py                       # bundled sample photos
    python scripts/evaluate_pipeline_cases.py --images People       # your own folder
    python scripts/evaluate_pipeline_cases.py --cases dark,noisy --level high

Without ``--images`` the scikit-image sample photographs are used (install
scikit-image for that). Output goes to ``.runtime/pipeline_eval/<run name>/``:
``summary.md``, ``results.json`` and one ``<photo>__<case>.jpg`` per case showing
clean | degraded input | enhanced output, plus ``sheet.jpg`` with all of them.

Real photographs, as they are (portraits, group photos), with the models this
machine has enabled:

    python scripts/evaluate_pipeline_cases.py --images gpu_sample/faces --cases original \
        --enable HD_RESTORATION_ENABLED,GFPGAN_ENABLED --limit 20 --name gpu-portraits

``--enable`` switches settings on for this run only. The summary opens with what
kind of evidence the run is: ``real_gpu_models`` (a learned model ran on CUDA or
MPS), ``real_cpu_models`` (a learned model ran on the CPU) or
``cpu_classical_only`` (no learned model ran; only OpenCV processing). It lists,
per photo, the identity outcome, each model stage's device and fallback reason,
latency, and peak host and GPU memory.

What this does and does not show: the degradations are synthetic and the numbers
come from whatever models this machine has installed (the summary lists which
stages actually ran). It is evidence about the pipeline's routing and guards and
the classical stages; it is not a visual sign-off of any checkpoint that did not
run.
"""
import argparse
import json
import os
import resource
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
CASES = (
    "original", "good", "dark", "noisy", "blurred", "compressed", "overexposed", "grayscale", "group", "transparent",
)
DEFAULT_CASES = tuple(case for case in CASES if case != "original")
# Stages that run a learned model. Everything else is classical OpenCV code.
MODEL_STAGES = (
    "purevision_restoration", "low_resolution_restoration", "hd_restoration",
    "purescale_super_resolution", "face_restoration", "colorization",
)


def gpu_memory_mb(reset=False):
    """Peak GPU memory since the last reset, in MB, or ``None`` without a GPU."""
    torch = sys.modules.get("torch")
    if torch is None:
        return None
    if torch.cuda.is_available():
        if reset:
            torch.cuda.reset_peak_memory_stats()
            return 0.0
        return round(torch.cuda.max_memory_allocated() / (1024 * 1024))
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available() and hasattr(torch, "mps"):
        return round(torch.mps.driver_allocated_memory() / (1024 * 1024))
    return None


def evidence_tier(rows):
    """What kind of evidence a run is, from the stages that actually ran."""
    devices = {device for row in rows for device in row["model_stages_run"].values()}
    if any(str(device).startswith(("cuda", "mps")) for device in devices):
        return "real_gpu_models"
    if devices or any(row["zero_dce_applied"] for row in rows):
        return "real_cpu_models"
    return "cpu_classical_only"


def load_sources(folder, limit):
    import cv2

    if folder:
        paths = sorted(p for p in Path(folder).expanduser().iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)[:limit]
        sources = [(path.stem, cv2.imread(str(path), cv2.IMREAD_COLOR)) for path in paths]
        return [(name, image) for name, image in sources if image is not None]
    try:
        from skimage import data
    except ImportError as error:
        raise SystemExit("Pass --images <folder>, or install scikit-image for the sample photos.") from error
    names = ("astronaut", "coffee", "chelsea", "rocket")[:limit]
    return [(name, cv2.cvtColor(getattr(data, name)(), cv2.COLOR_RGB2BGR)) for name in names]


def degrade(clean, case, rng):
    """Return ``(degraded BGR, alpha or None)`` for one case."""
    import cv2
    import numpy as np

    if case in {"good", "original"}:
        return clean.copy(), None
    if case == "dark":
        dark = (clean.astype(np.float32) / 255.0) ** 2.2 * 0.10  # two-plus stops under, in linear light
        dark = (dark ** (1 / 2.2)) * 255.0 + rng.normal(0, 3.0, clean.shape)
        return np.clip(dark, 0, 255).astype(np.uint8), None
    if case == "noisy":
        return np.clip(clean.astype(np.float32) + rng.normal(0, 14.0, clean.shape), 0, 255).astype(np.uint8), None
    if case == "blurred":
        return cv2.GaussianBlur(clean, (0, 0), 2.5), None
    if case == "compressed":
        encoded = cv2.imencode(".jpg", clean, [cv2.IMWRITE_JPEG_QUALITY, 12])[1]
        return cv2.imdecode(encoded, cv2.IMREAD_COLOR), None
    if case == "overexposed":
        return np.clip(clean.astype(np.float32) * 1.55 + 35, 0, 255).astype(np.uint8), None
    if case == "grayscale":
        return cv2.cvtColor(cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR), None
    if case == "group":
        top = np.hstack((clean, cv2.flip(clean, 1)))
        return np.vstack((top, cv2.flip(top, 1))), None
    if case == "transparent":
        height, width = clean.shape[:2]
        alpha = np.zeros((height, width), dtype=np.uint8)
        cv2.ellipse(alpha, (width // 2, height // 2), (int(width * 0.42), int(height * 0.42)), 0, 0, 360, 255, -1)
        alpha = cv2.GaussianBlur(alpha, (0, 0), max(2.0, width / 120))
        dim = np.clip(clean.astype(np.float32) * 0.45, 0, 255).astype(np.uint8)
        return dim, alpha
    raise ValueError(case)


def reference_for(clean, case):
    """The clean image a case should move toward, on that case's pixel grid."""
    import cv2
    import numpy as np

    if case == "group":
        top = np.hstack((clean, cv2.flip(clean, 1)))
        return np.vstack((top, cv2.flip(top, 1)))
    if case == "grayscale":
        return cv2.cvtColor(cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    return clean


def psnr(first, second):
    import numpy as np

    error = float(np.mean((first.astype(np.float32) - second.astype(np.float32)) ** 2))
    return 99.0 if error == 0 else round(float(10 * np.log10(255.0 ** 2 / error)), 2)


def write_crops(path, before, after):
    """Full-resolution face and background crops, before and after, side by side.

    Scaled-down panels hide exactly what matters here (texture, halos, colour
    blotches), so these are taken at the image's own resolution.
    """
    import cv2
    import numpy as np

    from enhancement.face_fidelity import _detect_boxes

    height, width = before.shape[:2]
    crops = []
    try:
        boxes = _detect_boxes(before)
    except Exception:  # no detector: background crop only
        boxes = []
    if boxes:
        x, y, w, h = boxes[0]
        left, top = int(max(0, (x - 0.15 * w) * width)), int(max(0, (y - 0.15 * h) * height))
        right, bottom = int(min(width, (x + 1.15 * w) * width)), int(min(height, (y + 1.15 * h) * height))
        crops.append(("face", (top, bottom, left, right)))
    side = max(64, min(height, width) // 4)
    crops.append(("background", (0, side, 0, side)))
    rows = []
    for label, (top, bottom, left, right) in crops:
        pair = [labelled(image[top:bottom, left:right], f"{label} {name}", 360) for image, name in ((before, "before"), (after, "after"))]
        rows.append(np.hstack(pair))
    widest = max(row.shape[1] for row in rows)
    sheet = np.vstack([cv2.copyMakeBorder(row, 0, 4, 0, widest - row.shape[1], cv2.BORDER_CONSTANT) for row in rows])
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])


def labelled(image, text, height):
    import cv2

    scale = height / image.shape[0]
    panel = cv2.resize(image, (max(1, round(image.shape[1] * scale)), height), interpolation=cv2.INTER_AREA)
    cv2.rectangle(panel, (0, 0), (panel.shape[1], 26), (0, 0, 0), -1)
    cv2.putText(panel, text, (6, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return panel


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", help="Folder of photos to use instead of the bundled samples.")
    parser.add_argument("--limit", type=int, default=4, help="How many source photos to use.")
    parser.add_argument("--cases", default=",".join(DEFAULT_CASES))
    parser.add_argument(
        "--enable", default="",
        help="Comma-separated boolean settings to switch on for this run, e.g. HD_RESTORATION_ENABLED,GFPGAN_ENABLED.",
    )
    parser.add_argument("--level", default="auto", choices=("auto", "low", "medium", "high"))
    parser.add_argument("--style", default="natural", choices=("natural", "dynamic", "ai_max"))
    parser.add_argument("--min-edge", type=int, default=0, help="Upscale smaller photos to this shorter edge first.")
    parser.add_argument("--max-edge", type=int, default=0, help="Reduce larger photos to this longer edge first.")
    parser.add_argument("--name", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--output", default=str(ROOT / ".runtime" / "pipeline_eval"))
    arguments = parser.parse_args()
    cases = [case.strip() for case in arguments.cases.split(",") if case.strip()]
    unknown = [case for case in cases if case not in CASES]
    if unknown:
        raise SystemExit(f"Unknown case(s): {', '.join(unknown)}. Choose from: {', '.join(CASES)}.")

    import django

    django.setup()
    import cv2
    import numpy as np
    from django.conf import settings

    enabled = [name.strip() for name in arguments.enable.split(",") if name.strip()]
    for name in enabled:
        if not hasattr(settings, name) or not isinstance(getattr(settings, name), bool):
            raise SystemExit(f"{name} is not a boolean setting.")
        setattr(settings, name, True)

    from enhancement.services import _attach_alpha, _structural_similarity, analyze_image_quality, enhance_bgr

    output_dir = Path(arguments.output) / arguments.name
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    rows, panels = [], []
    for name, clean in load_sources(arguments.images, arguments.limit):
        if arguments.min_edge and min(clean.shape[:2]) < arguments.min_edge:
            scale = arguments.min_edge / min(clean.shape[:2])
            clean = cv2.resize(
                clean, (round(clean.shape[1] * scale), round(clean.shape[0] * scale)), interpolation=cv2.INTER_LANCZOS4,
            )
        if arguments.max_edge and max(clean.shape[:2]) > arguments.max_edge:
            scale = arguments.max_edge / max(clean.shape[:2])
            clean = cv2.resize(clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        for case in cases:
            degraded, alpha = degrade(clean, case, rng)
            target = reference_for(clean, case)
            memory_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            gpu_memory_mb(reset=True)
            started = time.perf_counter()
            enhanced, level, parameters = enhance_bgr(degraded, arguments.level, style=arguments.style)
            elapsed = time.perf_counter() - started
            memory_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
            comparable = enhanced
            if enhanced.shape != target.shape:
                comparable = cv2.resize(enhanced, (target.shape[1], target.shape[0]), interpolation=cv2.INTER_AREA)
            before, after = analyze_image_quality(degraded), parameters["quality_after"]
            row = {
                "photo": name,
                "case": case,
                "megapixels": round(degraded.shape[0] * degraded.shape[1] / 1e6, 2),
                "level": level,
                "seconds": round(elapsed, 2),
                "peak_memory_mb": round(memory_after / divisor),
                "peak_memory_growth_mb": round((memory_after - memory_before) / divisor),
                "defects": before["defects"],
                "psnr_to_clean_before": psnr(degraded, target),
                "psnr_to_clean_after": psnr(comparable, target),
                "ssim_to_clean_before": round(_structural_similarity(degraded, target), 4),
                "ssim_to_clean_after": round(_structural_similarity(comparable, target), 4),
                "luminance": [before["mean_luminance"], after["mean_luminance"]],
                "noise": [before["estimated_noise"], after["estimated_noise"]],
                "sharpness": [before["laplacian_sharpness"], after["laplacian_sharpness"]],
                "highlight_clipping": [before["highlight_clipping_percent"], after["highlight_clipping_percent"]],
                "chroma_shift": parameters["quality_guards"]["guard_b"]["mean_chroma_shift"],
                "stages_that_changed_pixels": [
                    entry["stage"] for entry in parameters["stage_reports"] if entry["changed_pixels"]
                ],
                "fallbacks": {
                    entry["stage"]: entry["fallback_reason"]
                    for entry in parameters["stage_reports"]
                    if entry["status"] in {"fallback", "failed", "rejected"}
                },
                "face_fidelity": {
                    key: parameters["face_fidelity"].get(key) for key in ("status", "faces_checked", "faces_adjusted")
                },
                "identity_status": parameters["identity_status"],
                "identity": {
                    key: parameters["identity_verification"].get(key)
                    for key in ("status", "verified", "reason", "requirement_bypass", "face_restoration_unverified")
                } | {
                    key: parameters["identity_guard"].get(key)
                    for key in ("similarity", "initial_similarity", "faces_compared", "strength")
                },
                "gpu_peak_memory_mb": gpu_memory_mb(),
                "zero_dce_applied": bool(parameters["zero_dce"].get("applied")),
                "model_stages_run": {
                    entry["stage"]: entry.get("device") or next(
                        (m.get("device") for m in entry.get("models", []) if m.get("applied")), "unknown",
                    )
                    for entry in parameters["stage_reports"]
                    if entry["stage"] in MODEL_STAGES and entry["status"] in {"accepted", "partially_accepted"}
                },
                "stage_durations_ms": {
                    entry["stage"]: entry["duration_ms"] for entry in parameters["stage_reports"]
                },
                "no_correction_needed": parameters["no_correction_needed"],
                "minimal_processing": parameters["plan"]["minimal_processing"],
                "warnings": parameters["warnings"],
            }
            if alpha is not None:
                with_alpha = _attach_alpha(enhanced, alpha)
                row["alpha_preserved_exactly"] = bool(np.array_equal(with_alpha[:, :, 3], alpha))
            rows.append(row)
            panel = np.hstack([
                labelled(target, f"{name} / clean", 300),
                labelled(degraded, f"{case} input", 300),
                labelled(comparable, f"enhanced ({level})", 300),
            ])
            cv2.imwrite(str(output_dir / f"{name}__{case}.jpg"), panel, [cv2.IMWRITE_JPEG_QUALITY, 90])
            write_crops(output_dir / f"{name}__{case}__crops.jpg", degraded, comparable)
            panels.append(panel)
            print(
                f"{name:10s} {case:12s} {row['megapixels']:5.2f} MP {elapsed:5.1f}s "
                f"PSNR {row['psnr_to_clean_before']:5.1f}->{row['psnr_to_clean_after']:5.1f} "
                f"SSIM {row['ssim_to_clean_before']:.3f}->{row['ssim_to_clean_after']:.3f} "
                f"faces {row['face_fidelity']['faces_checked']} identity {row['identity_status']}"
            )

    width = max(panel.shape[1] for panel in panels)
    sheet = np.vstack([
        cv2.copyMakeBorder(panel, 0, 4, 0, width - panel.shape[1], cv2.BORDER_CONSTANT, value=(40, 40, 40))
        for panel in panels
    ])
    cv2.imwrite(str(output_dir / "sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    (output_dir / "results.json").write_text(json.dumps(rows, indent=2))

    tier = evidence_tier(rows)
    torch = sys.modules.get("torch")
    environment = [
        f"- Evidence: **`{tier}`** "
        + {
            "real_gpu_models": "(a learned model ran on a GPU)",
            "real_cpu_models": "(a learned model ran, on the CPU)",
            "cpu_classical_only": "(no learned model ran; OpenCV processing only)",
        }[tier],
        f"- PyTorch: {getattr(torch, '__version__', 'not loaded')}"
        + (f", CUDA device: {torch.cuda.get_device_name(0)}" if torch is not None and torch.cuda.is_available() else ""),
        f"- Settings switched on for this run: {', '.join(enabled) or 'none'}",
        f"- Model stages that ran: {sorted({stage for row in rows for stage in row['model_stages_run']}) or 'none'}",
    ]
    lines = [
        f"# Pipeline evaluation `{arguments.name}`",
        "",
        *environment,
        "",
        f"Level `{arguments.level}`, style `{arguments.style}`. Degradations are synthetic; PSNR and SSIM are "
        "measured against the clean photo, so a rise means the output moved toward it.",
        "",
        "| Photo | Case | MP | Time (s) | PSNR before | PSNR after | SSIM before | SSIM after | Noise | Clipped % | Faces checked/adjusted |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['photo']} | {row['case']} | {row['megapixels']} | {row['seconds']} | "
            f"{row['psnr_to_clean_before']} | {row['psnr_to_clean_after']} | "
            f"{row['ssim_to_clean_before']} | {row['ssim_to_clean_after']} | "
            f"{row['noise'][0]} -> {row['noise'][1]} | {row['highlight_clipping'][0]} -> {row['highlight_clipping'][1]} | "
            f"{row['face_fidelity']['faces_checked']}/{row['face_fidelity']['faces_adjusted']} |"
        )
    fallbacks = sorted({f"{stage}: {reason}" for row in rows for stage, reason in row["fallbacks"].items()})
    lines += ["", "## Stages that fell back", ""] + ([f"- {item}" for item in fallbacks] or ["- none"])
    lines += [
        "", "## Identity, devices and memory", "",
        "| Photo | Case | Identity | Similarity | Model stages (device) | Time (s) | GPU peak MB |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        stages = ", ".join(f"{stage} ({device})" for stage, device in row["model_stages_run"].items()) or "none"
        lines.append(
            f"| {row['photo']} | {row['case']} | {row['identity']['status']}"
            + (" (restored, unverified)" if row["identity"]["face_restoration_unverified"] else "")
            + f" | {row['identity']['similarity']} | {stages} | {row['seconds']} | {row['gpu_peak_memory_mb']} |"
        )
    lines += ["", f"Peak process memory: {max(row['peak_memory_mb'] for row in rows)} MB."]
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"\nWrote {output_dir}")


if __name__ == "__main__":
    main()

"""Run FaceCloud's production enhancement pipeline on local review samples."""

import json
import os
from pathlib import Path
import sys

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from enhancement.models import EnhancementLevel  # noqa: E402
from enhancement.services import analyze_image_quality, enhance_bgr  # noqa: E402


INPUT_DIR = ROOT / ".runtime" / "dataset_review" / "image"
OUTPUT_DIR = Path(os.environ.get(
    "FACECLOUD_EVALUATION_OUTPUT_DIR",
    ROOT / ".runtime" / "dataset_review" / "results",
)).resolve()
SAMPLES = tuple(
    value.strip()
    for value in os.environ.get(
        "FACECLOUD_EVALUATION_SAMPLES",
        "1834.png,3665.png,5720.png",
    ).split(",")
    if value.strip()
)


def compact_quality(metrics):
    keys = (
        "mean_luminance",
        "contrast_stddev",
        "laplacian_sharpness",
        "estimated_noise",
        "shadow_clipping_percent",
        "highlight_clipping_percent",
        "dark_pixel_percent",
        "bright_pixel_percent",
        "width",
        "height",
        "defects",
    )
    return {key: metrics.get(key) for key in keys}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "pipeline": "FaceCloud enhance_bgr",
        "requested_level": "auto",
        "samples": [],
    }
    for filename in SAMPLES:
        source_path = INPUT_DIR / filename
        source = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
        if source is None:
            raise RuntimeError(f"Could not decode {source_path}")
        before = analyze_image_quality(source)
        enhanced, applied_level, parameters = enhance_bgr(
            source, EnhancementLevel.AUTO
        )
        after = analyze_image_quality(enhanced)
        output_path = OUTPUT_DIR / f"{source_path.stem}_enhanced.png"
        if not cv2.imwrite(str(output_path), enhanced):
            raise RuntimeError(f"Could not write {output_path}")
        comparison_source = source
        if source.shape[:2] != enhanced.shape[:2]:
            comparison_source = cv2.resize(
                source,
                (enhanced.shape[1], enhanced.shape[0]),
                interpolation=cv2.INTER_CUBIC,
            )
        comparison = cv2.hconcat((comparison_source, enhanced))
        comparison_path = OUTPUT_DIR / f"{source_path.stem}_comparison.png"
        if not cv2.imwrite(str(comparison_path), comparison):
            raise RuntimeError(f"Could not write {comparison_path}")
        report["samples"].append({
            "filename": filename,
            "input_path": str(source_path.relative_to(ROOT)),
            "output_path": str(output_path.relative_to(ROOT)),
            "comparison_path": str(comparison_path.relative_to(ROOT)),
            "applied_level": str(applied_level),
            "algorithm": parameters.get("algorithm"),
            "before": compact_quality(before),
            "after": compact_quality(after),
            "quality_comparison": parameters.get("quality_comparison"),
            "zero_dce": parameters.get("zero_dce"),
            "restoration": parameters.get("restoration"),
            "face_restoration": parameters.get("face_restoration"),
            "warnings": parameters.get("warnings", []),
            "stage_timings_ms": parameters.get("stage_timings_ms", {}),
        })
    report_path = OUTPUT_DIR / "enhancement_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

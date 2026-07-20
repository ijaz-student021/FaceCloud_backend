"""Read-only quality audit for an image dataset stored in a ZIP archive."""

from __future__ import annotations

import hashlib
import io
import json
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageStat


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = round((len(ordered) - 1) * fraction)
    return float(ordered[index])


def main() -> None:
    archive_path = Path(sys.argv[1])
    formats: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    dimensions: Counter[tuple[int, int]] = Counter()
    hashes: defaultdict[str, list[str]] = defaultdict(list)
    corrupt: list[dict[str, str]] = []
    brightness: list[float] = []
    brightness_by_file: list[tuple[float, str]] = []
    widths: list[int] = []
    heights: list[int] = []
    files_with_detected_faces = 0
    detected_face_count = 0
    face_detector = cv2.CascadeClassifier(
        str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    )
    compressed_bytes = archive_path.stat().st_size

    with zipfile.ZipFile(archive_path) as archive:
        members = [entry for entry in archive.infolist() if not entry.is_dir()]
        image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
        image_members = [
            entry for entry in members
            if Path(entry.filename).suffix.lower() in image_extensions
        ]
        non_image_members = [
            entry.filename for entry in members if entry not in image_members
        ]
        total_uncompressed = sum(entry.file_size for entry in members)

        for entry in image_members:
            try:
                payload = archive.read(entry)
                hashes[hashlib.sha256(payload).hexdigest()].append(entry.filename)
                with Image.open(io.BytesIO(payload)) as image:
                    image.load()
                    formats[image.format or "UNKNOWN"] += 1
                    modes[image.mode] += 1
                    dimensions[image.size] += 1
                    widths.append(image.width)
                    heights.append(image.height)
                    sample = image.convert("RGB")
                    if not face_detector.empty():
                        grayscale = cv2.cvtColor(np.asarray(sample), cv2.COLOR_RGB2GRAY)
                        faces = face_detector.detectMultiScale(
                            grayscale, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
                        )
                        if len(faces):
                            files_with_detected_faces += 1
                            detected_face_count += len(faces)
                    sample.thumbnail((128, 128))
                    mean_brightness = float(ImageStat.Stat(sample.convert("L")).mean[0])
                    brightness.append(mean_brightness)
                    brightness_by_file.append((mean_brightness, entry.filename))
            except Exception as exc:  # audit must continue after a bad member
                corrupt.append({"file": entry.filename, "error": str(exc)})

    duplicate_groups = [names for names in hashes.values() if len(names) > 1]
    duplicate_files = sum(len(names) - 1 for names in duplicate_groups)
    valid_count = len(image_members) - len(corrupt)
    folder_counts = Counter(
        "/".join(Path(entry.filename).parts[:-1]) or "."
        for entry in image_members
    )
    result = {
        "archive": str(archive_path),
        "archive_bytes": compressed_bytes,
        "uncompressed_bytes": total_uncompressed,
        "file_count": len(members),
        "image_file_count": len(image_members),
        "non_image_files": non_image_members,
        "valid_image_count": valid_count,
        "corrupt_count": len(corrupt),
        "corrupt_examples": corrupt[:10],
        "formats": dict(formats),
        "color_modes": dict(modes),
        "unique_dimensions": len(dimensions),
        "most_common_dimensions": [
            {"size": f"{width}x{height}", "count": count}
            for (width, height), count in dimensions.most_common(10)
        ],
        "width": {
            "min": min(widths, default=0),
            "median": statistics.median(widths) if widths else 0,
            "max": max(widths, default=0),
        },
        "height": {
            "min": min(heights, default=0),
            "median": statistics.median(heights) if heights else 0,
            "max": max(heights, default=0),
        },
        "face_detection_proxy": {
            "files_with_detected_faces": files_with_detected_faces,
            "share_percent": round(
                100 * files_with_detected_faces / valid_count, 2
            ) if valid_count else 0,
            "detected_face_count": detected_face_count,
            "method": (
                "OpenCV Haar frontal-face proxy; review false positives/negatives manually"
                if not face_detector.empty()
                else "Unavailable: OpenCV installation has no Haar cascade data"
            ),
        },
        "brightness": {
            "min": round(min(brightness, default=0), 2),
            "p10": round(percentile(brightness, 0.10), 2),
            "median": round(statistics.median(brightness), 2) if brightness else 0,
            "p90": round(percentile(brightness, 0.90), 2),
            "max": round(max(brightness, default=0), 2),
            "very_dark_below_50": sum(value < 50 for value in brightness),
            "very_bright_above_205": sum(value > 205 for value in brightness),
        },
        "brightness_examples": [
            {"file": filename, "mean": round(value, 2)}
            for value, filename in (
                sorted(brightness_by_file)[:3]
                + sorted(brightness_by_file)[len(brightness_by_file) // 2 - 1 : len(brightness_by_file) // 2 + 2]
                + sorted(brightness_by_file)[-3:]
            )
        ],
        "exact_duplicate_groups": len(duplicate_groups),
        "extra_exact_duplicate_files": duplicate_files,
        "duplicate_examples": duplicate_groups[:10],
        "top_level_components": sorted(
            {entry.filename.split("/", 1)[0] for entry in members}
        ),
        "image_folder_counts": dict(folder_counts),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

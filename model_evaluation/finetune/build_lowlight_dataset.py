"""Collect the project's own low-light photographs into a training folder (copies; the originals and the database are not touched).

A photograph counts as low-light when its measured mean luminance is below 70 (0-255) or the quality check called it underexposed. Exact duplicates are dropped by SHA-256.
Copies are shrunk to a 1024 px long edge (the trainer takes 256 px crops anyway). The folder is private: it stays on this machine and is not committed.
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common import cv2, np  # noqa: E402
from quality_control.models import ImageQualityDecision  # noqa: E402

DEST = HERE / "zero_dce" / "dataset" / "image"
DEST.mkdir(parents=True, exist_ok=True)
for old in DEST.glob("*"):
    old.unlink()
seen, manifest = set(), []
for decision in ImageQualityDecision.objects.select_related("image").order_by("image_id"):
    image = decision.image
    if image.status == "trashed" or not image.original_file:
        continue
    luminance = (decision.measurements or {}).get("mean_luminance") if isinstance(decision.measurements, dict) else None
    if not ((luminance is not None and luminance < 70) or "underexposed" in (decision.rejection_reasons or [])):
        continue
    try:
        data = image.original_file.read()
    except Exception:
        continue
    digest = hashlib.sha256(data).hexdigest()
    if digest in seen:
        continue
    seen.add(digest)
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        continue
    scale = 1024 / max(frame.shape[:2])
    if scale < 1:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    name = f"{len(manifest):03d}_{digest[:8]}.jpg"
    cv2.imwrite(str(DEST / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    manifest.append({"file": name, "image_id": image.pk, "sha256": digest, "mean_luminance": luminance, "public_stock": "unsplash" in image.original_name.lower()})
(HERE / "zero_dce" / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2))
print(len(manifest), "low-light photographs;", sum(m["public_stock"] for m in manifest), "of them public stock")

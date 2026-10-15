"""Helpers shared by the evaluation scripts.

``pick_sample`` makes a reproducible sample, ``degrade`` makes a small, blurred,
compressed copy of a photograph (the kind of input the lightweight pipeline cannot
recover, used to check it neither invents detail nor damages identity), and
``identity_report`` reduces a result's identity-guard record to the fields worth
keeping. No embedding vector is ever read or returned here.
"""
import random
from pathlib import Path


def pick_sample(paths, count, seed):
    """A reproducible sample: the same seed always gives the same photos."""
    ordered = sorted(paths)
    return random.Random(seed).sample(ordered, min(count, len(ordered)))


def export_sample(photos, faces, destination):
    """Copy the sampled photos into ``destination/images`` and ``destination/faces``.

    Lets a sample travel to another machine without copying the whole dataset;
    run there with ``--images-dir destination/images``.
    """
    import shutil

    destination = Path(destination)
    for folder, paths in (("images", photos), ("faces", faces)):
        if paths:
            (destination / folder).mkdir(parents=True, exist_ok=True)
        for path in paths:
            shutil.copy2(path, destination / folder / Path(path).name)
    return len(photos), len(faces)


def identity_report(parameters):
    guard = parameters.get("identity_guard")
    if not isinstance(guard, dict):
        return None
    report = {key: guard.get(key) for key in ("status", "similarity", "initial_similarity", "faces_compared", "threshold")}
    report["reason"] = str(guard.get("fallback_reason") or "")[:120]
    return report


def degrade(image, shortest_edge=200, blur_sigma=1.5, jpeg_quality=35):
    """A small, blurred, compressed copy of ``image``.

    Used to ask whether the pipeline keeps a degraded face recognisable and does not
    invent detail: it does not restore the photo, and that is what is being checked.
    """
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    scale = min(1.0, shortest_edge / float(min(height, width)))
    small = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    blurred = cv2.GaussianBlur(small, (0, 0), blur_sigma)
    ok, encoded = cv2.imencode(".jpg", blurred, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return cv2.imdecode(np.frombuffer(encoded.tobytes(), dtype=np.uint8), cv2.IMREAD_COLOR) if ok else blurred

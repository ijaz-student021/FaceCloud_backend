"""Shared helpers for the model evaluation notebooks.

Everything here measures the models that FaceCloud actually runs, through the application's own functions, on the project's own photographs.
This is an evaluation of pretrained models (see README.md).
"""
import json
import os
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "outputs"
OUT.mkdir(exist_ok=True)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")   # a notebook kernel runs inside an event loop; these notebooks only read the database
warnings.filterwarnings("ignore")

import django  # noqa: E402

django.setup()

import cv2  # noqa: E402
import matplotlib  # noqa: E402
import numpy as np  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
from skimage.metrics import structural_similarity  # noqa: E402

from django.conf import settings  # noqa: E402
from uploads.models import ImageUpload  # noqa: E402

plt.rcParams.update({"figure.dpi": 110, "axes.grid": True, "grid.alpha": 0.25, "axes.spines.top": False, "axes.spines.right": False, "font.size": 10})
COLORS = {"input": "#8a8a8a", "model": "#e8590c", "baseline": "#1c7ed6", "good": "#2f9e44", "bad": "#e03131"}


def photo(pk, max_edge=900):
    """A project photograph (BGR) shrunk so its long edge is at most ``max_edge`` (the same scale for every model compared)."""
    upload = ImageUpload.objects.get(pk=pk)
    image = cv2.imdecode(np.frombuffer(upload.original_file.read(), np.uint8), cv2.IMREAD_COLOR)
    height, width = image.shape[:2]
    scale = max_edge / max(height, width)
    if scale < 1:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return image


def public_stock_ids(limit=None):
    """Ids of the project's public stock photographs (Unsplash files), so that no private photograph is ever drawn into a figure."""
    rows = ImageUpload.objects.filter(status="uploaded", original_name__icontains="unsplash").order_by("id").values_list("id", "original_name")
    seen, ids = set(), []
    for pk, name in rows:
        key = name.split("-unsplash")[0]          # one entry per Unsplash photograph (copies such as "..._enhanced" or duplicate uploads are the same picture)
        if key in seen:
            continue
        seen.add(key)
        ids.append(pk)
    return ids[:limit] if limit else ids


def gray(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def noise_sigma(image):
    """Robust noise estimate: median absolute deviation of the Laplacian residual (lower is cleaner)."""
    residual = cv2.Laplacian(gray(image).astype(np.float32), cv2.CV_32F)
    return float(np.median(np.abs(residual)) / 0.6745)


def sharpness(image):
    """Variance of the Laplacian (higher is sharper; noise also raises it, so read it together with noise_sigma)."""
    return float(cv2.Laplacian(gray(image), cv2.CV_64F).var())


def brightness(image):
    return float(gray(image).mean())


def ssim(reference, candidate):
    """Structural similarity of ``candidate`` to ``reference`` (both resized to the smaller common size)."""
    if reference.shape != candidate.shape:
        candidate = cv2.resize(candidate, (reference.shape[1], reference.shape[0]), interpolation=cv2.INTER_AREA)
    return float(structural_similarity(gray(reference), gray(candidate), data_range=255))


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return result, time.perf_counter() - started


def first(result):
    """Several model functions return ``(image, metadata)``: the image is the first element."""
    return result[0] if isinstance(result, tuple) else result


def montage(images, titles, columns=None, width=3.3, path=None):
    """Draw images side by side with titles (BGR in)."""
    columns = columns or len(images)
    rows = -(-len(images) // columns)
    figure, axes = plt.subplots(rows, columns, figsize=(width * columns, width * 0.72 * rows + 0.4))
    for axis, image, title in zip(np.atleast_1d(axes).ravel(), images, titles):
        axis.imshow(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        axis.set_title(title, fontsize=9)
        axis.axis("off")
    for axis in np.atleast_1d(axes).ravel()[len(images):]:
        axis.axis("off")
    figure.tight_layout()
    if path:
        figure.savefig(OUT / path, bbox_inches="tight")
    return figure


def save(figure, name):
    figure.savefig(OUT / name, bbox_inches="tight")
    return figure


def table(rows, columns=None):
    """Print a small fixed-width table (no pandas needed for the printed copy)."""
    columns = columns or list(rows[0].keys())
    widths = [max(len(str(c)), *(len(f"{r.get(c, '')}") for r in rows)) for c in columns]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(f"{row.get(c, '')}".ljust(w) for c, w in zip(columns, widths)))


def save_json(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2, default=str))


# ----- evaluation metrics (the ones used in every notebook) -----
def mse(a, b):
    return float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))


def psnr(a, b):
    """Peak signal-to-noise ratio in dB against a ground-truth image (higher = closer to the truth)."""
    error = mse(a, b)
    return 99.0 if error == 0 else float(10 * np.log10(255.0 ** 2 / error))


def mae(a, b):
    """Mean absolute pixel error against a ground-truth image (lower = closer)."""
    return float(np.mean(np.abs(a.astype(np.float64) - b.astype(np.float64))))


def scorecard(model, task, metrics, note=""):
    """Add this model's headline numbers to outputs/scorecard.json (read back by notebook 00)."""
    path = OUT / "scorecard.json"
    card = json.loads(path.read_text()) if path.exists() else {}
    card[model] = {"task": task, "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in metrics.items()}, "note": note}
    path.write_text(json.dumps(card, indent=2))
    return card[model]


# ----- faces: detector and identity embedding, as the application runs them -----
from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=1)
def face_detector():
    from validation.services import get_face_detector

    return get_face_detector()


@lru_cache(maxsize=1)
def identity_provider():
    from identities.providers import get_identity_provider

    return get_identity_provider()


def largest_face(image):
    """The biggest face YuNet finds: ``{"box": (x, y, w, h), "score", "landmarks"}`` or None."""
    found = face_detector().detect(image)
    if not len(found.boxes):
        return None
    index = int(np.argmax([box[2] * box[3] for box in found.boxes]))
    return {"box": tuple(found.boxes[index]), "score": float(found.scores[index]), "landmarks": found.landmarks[index]}


def face_embedding(image, face=None):
    """SFace embedding (128-d) of the largest face, or None when no face is found."""
    face = face or largest_face(image)
    if face is None or not face["landmarks"] or len(face["landmarks"]) != 5:
        return None
    x, y, w, h = face["box"]
    provider = identity_provider()
    prepared = provider.prepare_face(image, {"x": x, "y": y, "width": w, "height": h}, [{"x": a, "y": b} for a, b in face["landmarks"]])
    return provider.embed(prepared)


def identity_similarity(a, b):
    """SFace cosine similarity of two embeddings (the application's own measure)."""
    return float(identity_provider().similarity(a, b))


def iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ix, iy = max(0, min(ax + aw, bx + bw) - max(ax, bx)), max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter) if inter else 0.0


def degrade(image, kind, level, seed=0):
    """A controlled degradation of a clean photograph: blur (gaussian sigma), noise (sigma), dark (gamma divisor), jpeg (quality), small (shrink factor)."""
    if kind == "blur":
        return cv2.GaussianBlur(image, (0, 0), level)
    if kind == "noise":
        rng = np.random.default_rng(seed)
        return np.clip(image.astype(np.float32) + rng.normal(0, level, image.shape), 0, 255).astype(np.uint8)
    if kind == "dark":
        return np.clip(image.astype(np.float32) / level, 0, 255).astype(np.uint8)
    if kind == "jpeg":
        return cv2.imdecode(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(level)])[1], cv2.IMREAD_COLOR)
    if kind == "small":
        small = cv2.resize(image, None, fx=1 / level, fy=1 / level, interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR)
    raise ValueError(kind)


def auc_and_eer(same, different):
    """ROC AUC (probability a same-person pair outscores a different-person pair) and the equal error rate, from two lists of similarities."""
    same, different = np.asarray(same, float), np.asarray(different, float)
    greater = (same[:, None] > different[None, :]).mean() + 0.5 * (same[:, None] == different[None, :]).mean()
    best = (1.0, 0.5)
    for t in np.unique(np.concatenate([same, different])):
        fnr, fpr = (same < t).mean(), (different >= t).mean()
        if abs(fnr - fpr) < best[0]:
            best = (abs(fnr - fpr), (fnr + fpr) / 2)
    return float(greater), float(best[1])


def average_precision(labels, scores):
    order = np.argsort(-np.asarray(scores)); labels = np.asarray(labels)[order]
    hits = np.cumsum(labels); precision = hits / (np.arange(len(labels)) + 1)
    return float((precision * labels).sum() / max(labels.sum(), 1))

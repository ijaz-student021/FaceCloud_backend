"""The models that turn pictures and sentences into comparable vectors.

``Embedder`` is the small interface the rest of the feature uses; it never imports a machine-learning library. ``OpenClipEmbedder`` is the real one: it loads an
open_clip model (SigLIP ViT-B/16, Apache-2.0, see ``docs/model-licenses/semantic-search-model.md``) from a **local folder only**, after checking the weights file against a
SHA-256 recorded in the settings, and imports ``torch``/``open_clip`` the first time it is used (a deployment that never switches the feature on needs neither).
``FakeEmbedder`` lets the tests exercise indexing, ranking, owner scope and the switches without any model.
"""
import hashlib
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

WEIGHTS_FILE = "open_clip_model.safetensors"
RETRY_AFTER_SECONDS = 60       # a model that could not be loaded is not re-checked (the weights are hashed: ~1 GB) on every request


class SemanticSearchUnavailable(Exception):
    """The switch is on but the model cannot be used (not installed, files missing, checksum different). The message is safe to show."""


def normalise(vectors):
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def encode_vector(vector):
    """A float vector as the bytes stored in ``ImageEmbedding.vector`` (float16: half the space, no visible change in ranking)."""
    return np.asarray(vector, dtype="<f2").tobytes()


def decode_vectors(blobs, dimension):
    """``(n, dimension)`` float32 from stored bytes."""
    if not blobs:
        return np.zeros((0, dimension), dtype=np.float32)
    return np.stack([np.frombuffer(bytes(blob), dtype="<f2", count=dimension) for blob in blobs]).astype(np.float32)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OpenClipEmbedder:
    def __init__(self, model_dir, expected_sha256, name, device="auto"):
        self.device_choice = device
        self.model_dir = Path(model_dir) if model_dir else None
        self.expected_sha256 = (expected_sha256 or "").lower()
        self.name = name
        self.checksum = self.expected_sha256
        self.dimension = None
        self._lock = threading.Lock()
        self._model = self._preprocess = self._tokenizer = self._torch = self._device = None
        self._failure = None           # (message, monotonic time) of the last failed load

    def ready(self):
        """Load the model if that has not happened yet; raises ``SemanticSearchUnavailable`` with a message safe to show when it cannot be used."""
        self._load()

    def _load(self):
        with self._lock:
            if self._model is not None:
                return
            if self._failure and time.monotonic() - self._failure[1] < RETRY_AFTER_SECONDS:
                raise SemanticSearchUnavailable(self._failure[0])
            try:
                self._load_locked()
            except SemanticSearchUnavailable as error:
                self._failure = (str(error), time.monotonic())
                raise
            self._failure = None

    def _load_locked(self):
        if self.model_dir is None or not (self.model_dir / WEIGHTS_FILE).is_file():
            raise SemanticSearchUnavailable("The search-by-meaning model is not installed on this server.")
        if not self.expected_sha256:
            raise SemanticSearchUnavailable("SEMANTIC_SEARCH_MODEL_SHA256 is not set, so the model files cannot be trusted.")
        if file_sha256(self.model_dir / WEIGHTS_FILE) != self.expected_sha256:
            raise SemanticSearchUnavailable("The search-by-meaning model files are not the ones that were approved (checksum differs).")
        try:
            import open_clip
            import torch
        except ImportError as error:
            raise SemanticSearchUnavailable("The search-by-meaning libraries (torch, open_clip) are not installed on this server.") from error
        device = self.device_choice
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
        try:
            model, _train, preprocess = open_clip.create_model_and_transforms(f"local-dir:{self.model_dir}")
            tokenizer = open_clip.get_tokenizer(f"local-dir:{self.model_dir}")
        except Exception as error:
            logger.exception("Could not load the search-by-meaning model from %s.", self.model_dir)
            raise SemanticSearchUnavailable("The search-by-meaning model could not be loaded.") from error
        model.eval().to(device)
        self._torch, self._device = torch, device
        self._model, self._preprocess, self._tokenizer = model, preprocess, tokenizer
        self.dimension = int(model.encode_text(tokenizer(["probe"]).to(device)).shape[-1])
        logger.info("Search-by-meaning model %s loaded on %s (dimension %d).", self.name, device, self.dimension)

    def embed_images(self, images):
        self._load()
        torch = self._torch
        batch = torch.stack([self._preprocess(image) for image in images]).to(self._device)
        with self._lock, torch.inference_mode():
            features = self._model.encode_image(batch)
        return normalise(features.float().cpu().numpy())

    def embed_texts(self, texts):
        self._load()
        torch = self._torch
        tokens = self._tokenizer(list(texts)).to(self._device)
        with self._lock, torch.inference_mode():
            features = self._model.encode_text(tokens)
        return normalise(features.float().cpu().numpy())


class FakeEmbedder:
    """A tiny stand-in for tests: a picture is its nearest named colour, a sentence is the colour/object words it contains.

    Real photographs are never needed: solid-colour images are enough to prove that indexing, ranking, owner scope, the trash and the switches work.
    """

    name = "fake-colours"
    WORDS = ("red", "green", "blue", "yellow", "car", "beach", "cake", "dog")
    COLOURS = {"red": (220, 30, 30), "green": (30, 200, 30), "blue": (30, 30, 220), "yellow": (230, 220, 30)}

    def ready(self):
        pass

    def __init__(self, checksum="f" * 64):
        self.checksum = checksum
        self.dimension = len(self.WORDS)
        self.calls = {"images": 0, "texts": 0}

    def embed_images(self, images):
        self.calls["images"] += len(images)
        rows = []
        for image in images:
            rgb = np.asarray(image.convert("RGB").resize((1, 1))).reshape(3)
            nearest = min(self.COLOURS, key=lambda name: sum((int(a) - b) ** 2 for a, b in zip(rgb, self.COLOURS[name])))
            vector = np.full(len(self.WORDS), 0.05, dtype=np.float32)
            vector[self.WORDS.index(nearest)] = 1.0
            rows.append(vector)
        return normalise(rows)

    def embed_texts(self, texts):
        self.calls["texts"] += len(texts)
        rows = []
        for text in texts:
            vector = np.full(len(self.WORDS), 0.05, dtype=np.float32)
            for word in re.findall(r"[a-z]+", text.lower()):
                if word in self.WORDS:
                    vector[self.WORDS.index(word)] += 1.0
            rows.append(vector)
        return normalise(rows)

"""Search by what is in the photograph: index each accepted photograph once, rank a sentence against a person's own vectors.

Off by default (``SEMANTIC_SEARCH_ENABLED``). Everything here is owner-scoped, ignores photographs in the trash and photographs that were not accepted (or effectively accepted),
never logs a query's text, and compares only vectors made by the model that is configured now.
"""
import io
import logging
from functools import lru_cache

import numpy as np
from django.conf import settings
from PIL import Image

from quality_control.effective import effective_q
from search.models import ImageEmbedding
from uploads.models import ImageUpload

from .embedders import OpenClipEmbedder, SemanticSearchUnavailable, decode_vectors, encode_vector

logger = logging.getLogger(__name__)

THUMBNAIL_EDGE = 256          # the photograph is embedded from the same small JPEG the gallery shows: cheap to decode, and what the person sees
MIN_QUERY = 2
MAX_QUERY = 200
_embedder_override = None      # tests put a FakeEmbedder here


def enabled():
    return bool(settings.SEMANTIC_SEARCH_ENABLED)


def set_embedder_for_tests(embedder):
    global _embedder_override
    _embedder_override = embedder


@lru_cache(maxsize=1)
def _configured_embedder(model_dir, sha256, name, device):
    return OpenClipEmbedder(model_dir, sha256, name, device)


def get_embedder():
    """The embedder to use now. Raises ``SemanticSearchUnavailable`` when the feature is off or has no usable model."""
    if not enabled():
        raise SemanticSearchUnavailable("Search by what is in the photograph is switched off on this server.")
    if _embedder_override is not None:
        return _embedder_override
    return _configured_embedder(str(settings.SEMANTIC_SEARCH_MODEL_DIR or ""), settings.SEMANTIC_SEARCH_MODEL_SHA256, settings.SEMANTIC_SEARCH_MODEL_NAME,
                                settings.SEMANTIC_SEARCH_DEVICE)


def eligible_images():
    """Photographs worth indexing and searching: stored, not in the trash (the default manager), with a completed verdict that is (effectively) accepted."""
    return ImageUpload.objects.filter(status="uploaded", quality_decision__status="completed").filter(effective_q("accepted")).exclude(original_file="")


def image_for_embedding(image):
    """The small RGB picture the model sees. Raises ``ValueError`` when the file cannot be read."""
    from thumbnails.services import ThumbnailUnavailable, render_thumbnail

    try:
        payload, _width, _height = render_thumbnail(image.original_file, THUMBNAIL_EDGE)
    except ThumbnailUnavailable as error:
        raise ValueError(str(error)) from error
    return Image.open(io.BytesIO(payload)).convert("RGB")


def index_images(images, embedder=None, *, reindex=False):
    """Index ``images``; returns ``{"indexed": n, "skipped": n, "failed": n}``. Idempotent: a photograph already indexed by this model is skipped unless ``reindex``."""
    embedder = embedder or get_embedder()
    embedder.ready()
    images = list(images)
    done = {} if reindex else dict(ImageEmbedding.objects.filter(image__in=images, model_checksum=embedder.checksum).values_list("image_id", "pk"))
    todo = [image for image in images if image.pk not in done]
    pictures, kept = [], []
    failed = 0
    for image in todo:
        try:
            pictures.append(image_for_embedding(image))
            kept.append(image)
        except ValueError:
            failed += 1
            logger.warning("Photograph %s could not be read for search; it is not indexed.", image.pk)
    if kept:
        vectors = embedder.embed_images(pictures)
        for image, vector in zip(kept, vectors):
            ImageEmbedding.objects.update_or_create(
                image=image, defaults={
                    "owner_id": image.owner_id, "vector": encode_vector(vector), "dimension": len(vector), "model_name": getattr(embedder, "name", ""),
                    "model_checksum": embedder.checksum,
                },
            )
    return {"indexed": len(kept), "skipped": len(images) - len(todo), "failed": failed}


def pending_images(embedder=None, *, owner=None, reindex=False):
    """Eligible photographs with no vector from the current model (all of them with ``reindex``)."""
    embedder = embedder or get_embedder()
    queryset = eligible_images()
    if owner is not None:
        queryset = queryset.filter(owner=owner)
    if not reindex:
        queryset = queryset.exclude(semantic_embedding__model_checksum=embedder.checksum)
    return queryset.order_by("pk")


def rank(user, query, *, limit=None, embedder=None):
    """``[(image_id, score)]``, best first, for the sentence ``query`` against the person's own indexed photographs; ``score`` is the cosine similarity.

    Only vectors of the current model, only photographs that are eligible right now (not trashed, still accepted). Results below ``SEMANTIC_SEARCH_MIN_SCORE`` are dropped.
    """
    embedder = embedder or get_embedder()
    embedder.ready()                                    # a model that cannot be used says so, even for a person who has nothing indexed yet
    limit = min(limit or settings.SEMANTIC_SEARCH_DEFAULT_RESULTS, settings.SEMANTIC_SEARCH_MAX_RESULTS)
    rows = list(
        ImageEmbedding.objects.filter(owner=user, model_checksum=embedder.checksum, image__in=eligible_images())
        .values_list("image_id", "vector", "dimension")
    )
    if not rows:
        return []
    dimension = rows[0][2]
    matrix = decode_vectors([row[1] for row in rows if row[2] == dimension], dimension)
    ids = [row[0] for row in rows if row[2] == dimension]
    text = embedder.embed_texts([query])[0]
    scores = matrix @ text
    order = np.argsort(-scores)[:limit]
    floor = settings.SEMANTIC_SEARCH_MIN_SCORE
    return [(ids[index], float(scores[index])) for index in order if scores[index] >= floor]


def index_after_verdict(image):
    """Queue the indexing of ``image`` once its verdict is committed. Does nothing when the feature is off. Never raises: a search problem must not undo a verdict."""
    if not enabled():
        return
    try:
        from django.db import transaction

        from search.tasks import index_image_task

        transaction.on_commit(lambda: index_image_task.delay(image.pk))
    except Exception:
        logger.exception("Could not queue the search indexing of image_id=%s.", image.pk)

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="search.index_image", acks_late=True)
def index_image_task(image_id):
    """Index one photograph for search by meaning. Does nothing when the feature is off, the photograph is gone, in the trash or not accepted; never raises."""
    from search.semantic.embedders import SemanticSearchUnavailable
    from search.semantic.service import eligible_images, enabled, index_images

    if not enabled():
        return {"indexed": 0}
    try:
        image = eligible_images().filter(pk=image_id).first()
        if image is None:
            return {"indexed": 0}
        return index_images([image])
    except SemanticSearchUnavailable as error:
        logger.warning("Search indexing skipped for image_id=%s: %s", image_id, error)
    except Exception:
        logger.exception("Search indexing failed for image_id=%s.", image_id)
    return {"indexed": 0}


@shared_task(name="search.index_pending", acks_late=True)
def index_pending_task(limit=200):
    """Catch up: index up to ``limit`` accepted photographs that have no vector yet (moderator approvals, photographs from before the switch)."""
    from search.semantic.embedders import SemanticSearchUnavailable
    from search.semantic.service import enabled, get_embedder, index_images, pending_images

    if not enabled():
        return {"indexed": 0}
    try:
        embedder = get_embedder()
        return index_images(list(pending_images(embedder)[:limit]), embedder)
    except SemanticSearchUnavailable as error:
        logger.warning("Search catch-up skipped: %s", error)
    return {"indexed": 0}

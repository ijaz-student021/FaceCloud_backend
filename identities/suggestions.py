"""Two of one person's folders that look like one person, suggested to that person.

Photographs of a batch are grouped one after the other, so the first ones can open separate folders before later ones show they are the same person. Once a
batch has been fully grouped the owner is told, and ``GET /api/identities/groups/suggested-merges/`` lists the pairs. Nothing is merged for them: the existing
``POST /api/identities/groups/{id}/merge/`` stays the only way, and nothing is stored: a suggestion is worked out again on request.

Only the owner's own active folders are ever compared (never another person's), only folders made by the same identity model, and a pair is suggested when the
average faces are at least ``merge_suggestion_minimum()`` alike (``MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY`` when set, otherwise the model's own figure; the
same one the moderators' Identity Review screen uses).
"""
import logging

import numpy as np

from .models import IdentityGroup, IdentityMembership
from .providers import merge_suggestion_minimum
from .storage import reveal_embedding

logger = logging.getLogger(__name__)

LIMIT = 50
INACTIVE = (IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED)


def _brief(folder):
    return {"id": folder.pk, "display_name": folder.display_name, "member_count": folder.member_count}


def suggested_merges(owner, *, touching=None, limit=LIMIT):
    """Pairs of ``owner``'s folders that are alike, most alike first: ``[{"source_group", "target_group", "similarity", "source", "target"}]``.

    ``source_group`` is the folder to merge away (the one with fewer photographs, the newer on a tie) and ``target_group`` the one to keep; ``similarity`` is the
    cosine similarity of their average faces (0 to 1). ``touching`` (a set of folder ids) keeps only the pairs that involve one of those folders.
    """
    minimum = merge_suggestion_minimum()
    folders = list(IdentityGroup.objects.filter(owner=owner, state=IdentityGroup.State.ACTIVE).exclude(protected_centroid=b"").order_by("pk"))
    buckets = {}
    for folder in folders:
        buckets.setdefault((folder.provider, folder.embedding_dimension, folder.model_checksum), []).append(folder)
    pairs = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        vectors = np.asarray([reveal_embedding(folder.protected_centroid, folder.embedding_dimension) for folder in members], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms
        similarity = vectors @ vectors.T
        first, second = np.where(np.triu(similarity >= minimum, k=1))
        for i, j in zip(first.tolist(), second.tolist()):
            one, other = members[i], members[j]
            if touching is not None and one.pk not in touching and other.pk not in touching:
                continue
            keep, away = sorted((one, other), key=lambda folder: (-folder.member_count, folder.pk))
            pairs.append((float(similarity[i, j]), away, keep))
    pairs.sort(key=lambda item: (-item[0], item[1].pk, item[2].pk))
    return [
        {"source_group": away.pk, "target_group": keep.pk, "similarity": round(value, 4), "source": _brief(away), "target": _brief(keep)}
        for value, away, keep in pairs[:limit]
    ]


def batch_folder_ids(batch):
    """The folders that hold a face from a photograph of ``batch``."""
    return set(
        IdentityMembership.objects.filter(identity_face__image__batch=batch).exclude(state__in=INACTIVE)
        .exclude(identity_group__isnull=True).values_list("identity_group_id", flat=True)
    )


def suggested_pairs_of_batch(batch):
    """All the suggestions that involve a folder this batch put photographs in (the owner's other, older pairs are not this batch's news)."""
    touched = batch_folder_ids(batch)
    return suggested_merges(batch.owner, touching=touched, limit=None) if touched else []


def folders_in(pairs):
    """The distinct folder ids named by ``pairs``."""
    return {folder for pair in pairs for folder in (pair["source_group"], pair["target_group"])}


def announce_suggested_merges(batch_id):
    """Tell the owner, once per batch, that some of their folders may be the same person; returns the notification or ``None``.

    Only when the batch's grouping has finished (``verdict.batch_grouping_finished``) and some pair involving one of its folders is suggested. The notification's key
    is the batch, so however many analyses finish and however often this runs, the owner is told once.
    """
    from notifications.events import notify_folders_may_match
    from uploads.models import UploadBatch

    from .verdict import batch_grouping_finished

    batch = UploadBatch.objects.select_related("owner").filter(pk=batch_id).first()
    if batch is None or not batch_grouping_finished(batch):
        return None
    pairs = suggested_pairs_of_batch(batch)
    return notify_folders_may_match(batch, pairs) if pairs else None

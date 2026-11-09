"""The Identity Review screen: the organization's person folders, folders that look like one person, and merging two of them.

Scope, on purpose narrow: only folders that hold at least one photograph **submitted to this organization** (never a personal workspace), and a merge only
between two such folders of the **same owner** (the people in one member's own folders; a moderator never joins two different members' folders). Moderators
see the folders through the same preview URLs as the rest of the console.
"""
from itertools import combinations

import numpy as np
from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse
from rest_framework.exceptions import ValidationError

from identities.models import IdentityGroup, IdentityMembership
from identities.providers import merge_suggestion_minimum
from identities.storage import reveal_embedding
from organizations.models import OrganizationAuditEvent
from organizations.services import record_audit_event
from uploads.models import ImageUpload

INACTIVE = (IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED)
COVER_LIMIT = 3
SUGGESTION_LIMIT = 20


def _organization_photos(organization):
    return ImageUpload.objects.none() if organization.is_personal else ImageUpload.objects.filter(batch__organization=organization)


def organization_folders(organization, scope=None):
    """Active person folders that hold a photograph submitted to ``organization``, with how many of those photographs each holds.

    A folder belongs to one member (its owner), so ``scope`` (a ``DataScope``) removes whole folders of members the viewer may not see."""
    photos = _organization_photos(organization)
    present = IdentityMembership.objects.filter(
        identity_group=OuterRef("pk"), identity_face__image__in=photos,
    ).exclude(state__in=INACTIVE)
    folders = IdentityGroup.objects.filter(state=IdentityGroup.State.ACTIVE).filter(Exists(present))
    if scope is not None:
        folders = scope.exclude(folders)
    return (
        folders
        .select_related("owner")
        .annotate(photos_in_organization=Count(
            "memberships__identity_face__image",
            filter=Q(memberships__identity_face__image__in=photos) & ~Q(memberships__state__in=INACTIVE), distinct=True,
        ))
        .order_by("-updated_at", "-pk")
    )


def preview_url(request, organization, image_id):
    path = reverse("moderation:image-preview", kwargs={"organization_id": organization.pk, "image_id": image_id})
    return request.build_absolute_uri(path) if request is not None else path


def describe_folders(folders, organization, request=None):
    """Plain dicts for ``folders`` (a page): name, owner, counts, average match as a percentage, and up to three cover photographs."""
    folders = list(folders)
    photos = _organization_photos(organization)
    members = (
        IdentityMembership.objects.filter(identity_group__in=folders, identity_face__image__in=photos)
        .exclude(state__in=INACTIVE).select_related("identity_face__image").order_by("-similarity_score", "pk")
    )
    covers, scores = {}, {}
    for member in members:
        image = member.identity_face.image
        bucket = covers.setdefault(member.identity_group_id, [])
        if image.status == "uploaded" and image.pk not in {item["id"] for item in bucket} and len(bucket) < COVER_LIMIT:
            bucket.append({"id": image.pk, "preview_url": preview_url(request, organization, image.pk)})
        if member.similarity_score is not None:
            scores.setdefault(member.identity_group_id, []).append(member.similarity_score)
    return [{
        "id": folder.pk, "display_name": folder.display_name,
        "owner": {"id": folder.owner_id, "username": folder.owner.username},
        "member_count": folder.member_count, "photos_in_organization": folder.photos_in_organization,
        "matching_confidence": round(100 * sum(scores[folder.pk]) / len(scores[folder.pk]), 1) if scores.get(folder.pk) else None,
        "cover_images": covers.get(folder.pk, []), "updated_at": folder.updated_at,
    } for folder in folders]


def _centroid(folder):
    if not folder.protected_centroid:
        return None
    vector = np.asarray(reveal_embedding(folder.protected_centroid, folder.embedding_dimension), dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else None


def suggested_merges(organization, request=None, scope=None):
    """Pairs of one member's folders whose average faces are alike, most alike first (cosine similarity as a percentage).

    Same owner, same identity model, and at least ``MODERATOR_MERGE_SUGGESTION_MIN_SIMILARITY``. ``source`` is the folder to merge away (the one with fewer
    photographs, the newer on a tie) and ``target`` the one to keep. Nothing is stored: a suggestion is recomputed on request.
    """
    minimum = merge_suggestion_minimum()
    by_owner = {}
    for folder in organization_folders(organization, scope):
        by_owner.setdefault(folder.owner_id, []).append(folder)
    pairs = []
    for folders in by_owner.values():
        vectors = {folder.pk: _centroid(folder) for folder in folders}
        for first, second in combinations(folders, 2):
            a, b = vectors[first.pk], vectors[second.pk]
            if a is None or b is None or (first.provider, first.embedding_dimension, first.model_checksum) != (second.provider, second.embedding_dimension, second.model_checksum):
                continue
            similarity = float(np.dot(a, b))
            if similarity >= minimum:
                # Keep the folder with more photographs (the older one on a tie) and merge the other into it.
                keep, away = sorted((first, second), key=lambda folder: (-folder.member_count, folder.pk))
                pairs.append((similarity, away, keep))
    pairs.sort(key=lambda item: -item[0])
    pairs = pairs[:SUGGESTION_LIMIT]
    described = {item["id"]: item for item in describe_folders({folder for _s, one, two in pairs for folder in (one, two)}, organization, request)}
    return [{"similarity": round(100 * similarity, 1), "source": described[first.pk], "target": described[second.pk]} for similarity, first, second in pairs]


def merge_folders(*, organization, actor, source_group_id, target_group_id, reason="", request=None, scope=None):
    """Merge one folder into another; both must be this organization's folders of the same owner. Returns ``(source, target)``."""
    from identities.management import merge_groups

    if source_group_id == target_group_id:
        raise ValidationError({"detail": "A person folder cannot be merged into itself."})
    folders = {folder.pk: folder for folder in organization_folders(organization, scope).filter(pk__in=[source_group_id, target_group_id])}
    if len(folders) != 2:
        raise ValidationError({"detail": "Both person folders must hold photographs submitted to this organization."})
    source, target = folders[source_group_id], folders[target_group_id]
    if source.owner_id != target.owner_id:
        raise ValidationError({"detail": "These person folders belong to different people and cannot be merged."})
    merged_source, merged_target = merge_groups(source, target, source.owner, reason, actor=actor)
    record_audit_event(
        organization, actor, OrganizationAuditEvent.Action.IDENTITY_GROUPS_MERGED_BY_MODERATOR,
        target_type="IdentityGroup", target_id=merged_target.pk,
        new_value={"source_group_id": merged_source.pk, "target_group_id": merged_target.pk}, reason=reason, request=request,
    )
    return merged_source, merged_target

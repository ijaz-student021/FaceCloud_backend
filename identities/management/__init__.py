from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..models import IdentityChange, IdentityGroup, IdentityMembership
from ..services import _confirmed_states, _refresh_group, delete_folder, prune_empty_folders, renumber_person_folders


def _record(owner, actor, action, *, group=None, membership=None, before=None, after=None, reason=""):
    change = IdentityChange.objects.create(owner=owner, actor=actor, action=action, group=group, membership=membership, before=before or {}, after=after or {}, reason=reason)
    from activity.services import record_activity_event
    record_activity_event(
        owner, "group", f"group_{action}",
        object_type="IdentityGroup", object_id=group.pk if group else "",
        details={"actor_id": actor.pk if actor else None, "reason": reason},
    )
    return change


@transaction.atomic
def rename_group(group, actor, display_name):
    group = IdentityGroup.objects.select_for_update().get(pk=group.pk, owner=actor)
    cleaned = " ".join(display_name.split()).strip()
    if not cleaned:
        raise ValidationError({"display_name": "A folder name is required."})
    if IdentityGroup.objects.filter(owner=actor, display_name__iexact=cleaned).exclude(pk=group.pk).exists():
        raise ValidationError({"display_name": "You already have a person folder with this name."})
    before = group.display_name
    group.display_name = cleaned
    group.save(update_fields=["display_name", "updated_at"])
    _record(actor, actor, IdentityChange.Action.RENAMED, group=group, before={"display_name": before}, after={"display_name": cleaned})
    return group


@transaction.atomic
def merge_groups(source, target, reviewer, reason="", *, actor=None):
    """Merge ``source`` into ``target``.

    ``reviewer`` is always both groups' real owner -- the internal
    ``owner=reviewer`` lookup below is a real authorization gate, unchanged.
    A moderator merging on the owner's behalf (T-007) passes the groups'
    actual owner as ``reviewer`` and themself as ``actor``: IdentityChange
    stays attributed the same way review_membership()'s reviewer/actor split
    already works (see that function's docstring for the full reasoning).
    The self-service merge endpoint is unaffected -- it always passes the
    same user as both, so ``actor`` defaults to ``reviewer``.
    """
    actor = actor or reviewer
    if source.pk == target.pk:
        raise ValidationError("A person folder cannot be merged into itself.")
    groups = {row.pk: row for row in IdentityGroup.objects.select_for_update().filter(owner=reviewer, pk__in=[source.pk, target.pk])}
    if len(groups) != 2:
        raise ValidationError("Both person folders must belong to the current user.")
    source, target = groups[source.pk], groups[target.pk]
    if source.state != IdentityGroup.State.ACTIVE or target.state != IdentityGroup.State.ACTIVE:
        raise ValidationError("Only active person folders can be merged.")
    incompatible = (source.provider, source.embedding_dimension, source.model_checksum) != (target.provider, target.embedding_dimension, target.model_checksum)
    if incompatible:
        raise ValidationError("Person folders created by incompatible identity models cannot be merged.")
    moved = source.memberships.exclude(state__in=[IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED]).update(identity_group=target)
    if target.profile_membership_id is None and source.profile_membership_id:
        target.profile_membership_id = source.profile_membership_id
    source.profile_membership = None
    source.state = IdentityGroup.State.MERGED
    source.save(update_fields=["profile_membership", "state", "updated_at"])
    _refresh_group(source)
    _refresh_group(target)
    target.save(update_fields=["profile_membership", "updated_at"])
    _record(reviewer, actor, IdentityChange.Action.MERGED, group=target, before={"source_group_id": source.pk, "source_name": source.display_name}, after={"target_group_id": target.pk, "moved_memberships": moved}, reason=reason)
    return source, target


@transaction.atomic
def move_membership(membership, target, actor, reason=""):
    membership = IdentityMembership.objects.select_for_update(of=("self",)).select_related("identity_group").get(pk=membership.pk, owner=actor)
    target = IdentityGroup.objects.select_for_update().get(pk=target.pk, owner=actor, state=IdentityGroup.State.ACTIVE)
    source = membership.identity_group
    if source and source.pk == target.pk:
        return membership
    face = membership.identity_face
    if (face.provider, face.embedding_dimension, face.model_checksum) != (target.provider, target.embedding_dimension, target.model_checksum):
        raise ValidationError("The face and target folder use incompatible identity models.")
    if source and source.profile_membership_id == membership.pk:
        source.profile_membership = None
        source.save(update_fields=["profile_membership", "updated_at"])
    membership.identity_group = target
    membership.state = IdentityMembership.State.CONFIRMED
    membership.reviewed_by = actor
    membership.reviewed_at = timezone.now()
    membership.save()
    if source:
        _refresh_group(source)
    _refresh_group(target)
    _record(actor, actor, IdentityChange.Action.MOVED, group=target, membership=membership, before={"group_id": source.pk if source else None}, after={"group_id": target.pk}, reason=reason)
    if source:
        prune_empty_folders([source], actor=actor)          # the last photograph left it: the folder goes, the numbering closes up
    return membership


@transaction.atomic
def remove_membership(membership, actor, reason=""):
    membership = IdentityMembership.objects.select_for_update(of=("self",)).select_related("identity_group").get(pk=membership.pk, owner=actor)
    group = membership.identity_group
    if membership.state == IdentityMembership.State.REMOVED:
        return membership
    before = membership.state
    if group and group.profile_membership_id == membership.pk:
        group.profile_membership = None
        group.save(update_fields=["profile_membership", "updated_at"])
    membership.state = IdentityMembership.State.REMOVED
    membership.reviewed_by = actor
    membership.reviewed_at = timezone.now()
    membership.save()
    if group:
        _refresh_group(group)
    _record(actor, actor, IdentityChange.Action.REMOVED, group=group, membership=membership, before={"state": before}, after={"state": membership.state}, reason=reason)
    if group and prune_empty_folders([group], actor=actor)["deleted"]:
        membership.refresh_from_db()          # the folder it was removed from is gone, so the record now says "rejected" with no folder
    return membership


@transaction.atomic
def set_profile_membership(group, membership, actor):
    group = IdentityGroup.objects.select_for_update().get(pk=group.pk, owner=actor, state=IdentityGroup.State.ACTIVE)
    membership = IdentityMembership.objects.get(pk=membership.pk, owner=actor, identity_group=group, state__in=_confirmed_states())
    before = group.profile_membership_id
    group.profile_membership = membership
    group.save(update_fields=["profile_membership", "updated_at"])
    _record(actor, actor, IdentityChange.Action.PROFILE_SET, group=group, membership=membership, before={"membership_id": before}, after={"membership_id": membership.pk})
    return group


@transaction.atomic
def delete_empty_group(group, actor, reason="", *, dissolve=False):
    """Delete a person folder, and close the gap in the automatic numbering (``Person 004`` becomes ``Person 002``).

    A folder that still has photographs is refused unless ``dissolve`` is true: then the folder goes and its photographs stay in the library, in no folder.
    """
    group = IdentityGroup.objects.select_for_update().get(pk=group.pk, owner=actor)
    confirmed = group.memberships.filter(state__in=_confirmed_states()).count()
    if confirmed and not dissolve:
        raise ValidationError("Remove or move every confirmed image before deleting this person folder.")
    change = delete_folder(group, actor=actor, reason=reason)
    renumber_person_folders(actor)
    return change

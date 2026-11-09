"""Re-run the grouping of one owner's faces with the current limits, without losing anything the owner decided.

``manage.py regroup_identities --owner <id>`` prints what would change (a dry run, the default); ``--apply`` makes the change.

What it does: the owner's faces that the *system* placed (states auto_matched, new_group, or needs_review that nobody has looked at), in the order the
photographs were uploaded, are placed again with ``identities.services.decide_group`` and the provider's current limits. A folder that was left over
from the old limits (the same man in Person 005, 006 and 007) ends up as one; a face the old limits parked for review may now join its folder.

What it never touches: a folder the owner renamed, any folder that holds a face the owner confirmed, moved or reviewed by hand (or one set as the
folder's profile photo), and those faces themselves. They are reported as left alone. Nothing is deleted: a folder whose faces all moved is **merged**
into the folder that took most of them (state ``merged``, id still resolvable) or **archived** when none was taken; every move is written to the
folder's history (``IdentityChange``) with no actor, and to the owner's activity log.
"""
from collections import Counter

import numpy as np
from django.conf import settings
from django.db import transaction

from organizations.services import get_default_organization

from .management import merge_groups
from .models import IdentityChange, IdentityGroup, IdentityMembership
from .providers import thresholds_for
from .services import _confirmed_states, _next_group_name, _refresh_group, decide_group, folder_is_renamed
from .storage import reveal_embedding

INACTIVE = (IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED)
SYSTEM_STATES = (IdentityMembership.State.AUTO_MATCHED, IdentityMembership.State.NEW_GROUP, IdentityMembership.State.NEEDS_REVIEW)
DECISION_STATE = {"new": IdentityMembership.State.NEW_GROUP, "match": IdentityMembership.State.AUTO_MATCHED, "review": IdentityMembership.State.NEEDS_REVIEW}
# "auto_matched" and "new_group" only say how a placed face got there; a face already in the right folder under either label is not a change.
LABELS_OF_A_PLACED_FACE = (IdentityMembership.State.AUTO_MATCHED, IdentityMembership.State.NEW_GROUP)
METHOD = "regroup_v1"
REASON = "Regrouped with the current limits."


def _unit(vector):
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _protected_reason(group, memberships):
    if folder_is_renamed(group):
        return "renamed by the owner"
    if group.profile_membership_id:
        return "has a profile photo chosen by the owner"
    if any(m.reviewed_by_id or m.state == IdentityMembership.State.CONFIRMED for m in memberships):
        return "holds a face the owner confirmed or moved"
    return None


def plan_regroup(owner, provider):
    """What re-grouping ``owner``'s faces with ``provider``'s current limits would do (nothing is changed)."""
    limits = thresholds_for(provider)
    rows = list(
        IdentityMembership.objects.filter(owner=owner, identity_face__provider=provider.name, identity_face__embedding_dimension=provider.dimension)
        .exclude(state__in=INACTIVE).select_related("identity_face__image", "identity_group")
    )
    by_group = {}
    for row in rows:
        if row.identity_group_id:
            by_group.setdefault(row.identity_group_id, []).append(row)
    groups = {group.pk: group for group in IdentityGroup.objects.filter(owner=owner, pk__in=by_group, state=IdentityGroup.State.ACTIVE)}
    protected = {}
    for pk, group in groups.items():
        reason = _protected_reason(group, by_group[pk])
        if reason:
            protected[pk] = reason
    regroupable = [
        row for row in rows
        if row.state in SYSTEM_STATES and not row.reviewed_by_id and row.identity_group_id not in protected and (row.identity_group_id in groups or row.identity_group_id is None)
    ]
    regroupable.sort(key=lambda row: (row.identity_face.image.created_at, row.identity_face.image_id, row.identity_face.face_index))

    # Place the faces again, in upload order, on a clean slate (the left-alone folders are not candidates: they are not modified, not even added to).
    members, centroids, placed = [], [], []
    for row in regroupable:
        face = row.identity_face
        vector = _unit(np.asarray(reveal_embedding(face.protected_embedding, face.embedding_dimension), dtype=np.float32))
        scored = sorted(((float(np.dot(vector, centroid)), index) for index, centroid in enumerate(centroids)), reverse=True)
        decision, folder, best, second = decide_group(scored, limits)
        if decision == "new":
            members.append([vector])
            centroids.append(_unit(np.mean(members[-1], axis=0)))
            folder = len(members) - 1
        elif decision == "match":
            members[folder].append(vector)
            centroids[folder] = _unit(np.mean(members[folder], axis=0))
        placed.append({"row": row, "decision": decision, "folder": folder, "best": best, "second": second})

    # Keep existing folder ids where the new folders overlap the old ones: the largest overlap claims the old folder first, each old folder is claimed once.
    overlaps = []
    for index in range(len(members)):
        counts = Counter(item["row"].identity_group_id for item in placed if item["folder"] == index and item["decision"] != "review" and item["row"].identity_group_id)
        for group_id, count in counts.items():
            overlaps.append((-count, group_id, index))
    claimed, folder_group = {}, {}
    for _negative, group_id, index in sorted(overlaps):
        if index in folder_group or group_id in claimed:
            continue
        folder_group[index] = group_id
        claimed[group_id] = index
    new_folders = [index for index in range(len(members)) if index not in folder_group]

    moves = []
    for item in placed:
        row, index = item["row"], item["folder"]
        target = folder_group.get(index) if index is not None else None
        target_key = target if target is not None else (f"new:{index}" if index is not None and item["decision"] != "review" else None)
        if item["decision"] == "review" and index is not None:
            target_key = folder_group.get(index, f"new:{index}")
        new_state = DECISION_STATE[item["decision"]]
        same_place = row.identity_group_id == target_key
        same_kind = row.state == new_state or (row.state in LABELS_OF_A_PLACED_FACE and new_state in LABELS_OF_A_PLACED_FACE)
        if not (same_place and same_kind):
            moves.append({
                "membership_id": row.pk, "image_id": row.identity_face.image_id, "from_group": row.identity_group_id, "to_group": target_key,
                "from_state": row.state, "to_state": new_state,
            })
    touched = {row["from_group"] for row in moves if row["from_group"]}
    emptied = {}
    for group_id in touched:
        if group_id in claimed:
            continue
        taken = Counter(move["to_group"] for move in moves if move["from_group"] == group_id and isinstance(move["to_group"], int) and move["to_state"] != "needs_review")
        emptied[group_id] = taken.most_common(1)[0][0] if taken else None
    before_sizes = {pk: sum(1 for row in by_group[pk] if row.state in _confirmed_states()) for pk in groups if pk not in protected}
    after_sizes = Counter(item["folder"] for item in placed if item["decision"] != "review")
    return {
        "owner_id": owner.pk, "provider": provider.name, "limits": limits,
        "left_alone": [{"id": pk, "name": groups[pk].display_name, "reason": reason, "photos": len(by_group[pk])} for pk, reason in sorted(protected.items())],
        "faces_total": len(rows), "faces_regrouped": len(regroupable),
        "before": {"folders": len(before_sizes), "needs_review": sum(1 for row in regroupable if row.state == IdentityMembership.State.NEEDS_REVIEW),
                   "sizes": sorted(before_sizes.values(), reverse=True)},
        "after": {"folders": len(members), "needs_review": sum(1 for item in placed if item["decision"] == "review"), "sizes": sorted(after_sizes.values(), reverse=True),
                  "new_folders": len(new_folders)},
        "moves": moves, "merge_or_archive": [{"folder": group_id, "into": target} for group_id, target in sorted(emptied.items())],
        "_placed": placed, "_folder_group": folder_group, "_members": members,
    }


def public(plan):
    """The plan without the working data ``apply_regroup`` needs (what the command prints and what tests compare)."""
    return {key: value for key, value in plan.items() if not key.startswith("_")}


@transaction.atomic
def apply_regroup(plan, owner):
    """Carry out ``plan`` (from ``plan_regroup``) for ``owner``. Returns a summary of what was done."""
    from activity.services import record_activity_event

    from django.contrib.auth import get_user_model

    get_user_model().objects.select_for_update().get(pk=owner.pk)
    folder_group = dict(plan["_folder_group"])
    created = {}
    for index in range(len(plan["_members"])):
        if index in folder_group:
            continue
        sample = next(item["row"].identity_face for item in plan["_placed"] if item["folder"] == index)
        group = IdentityGroup.objects.create(
            owner=owner, organization=get_default_organization(owner), display_name=_next_group_name(owner), embedding_dimension=sample.embedding_dimension,
            provider=sample.provider, model_checksum=sample.model_checksum, policy_version=settings.IDENTITY_POLICY_VERSION,
        )
        folder_group[index] = group.pk
        created[index] = group.pk

    def resolve(key):
        if isinstance(key, str) and key.startswith("new:"):
            return folder_group[int(key.split(":")[1])]
        return key

    touched = set(created.values())
    moved = 0
    moves = {move["membership_id"]: move for move in plan["moves"]}
    for item in plan["_placed"]:
        move = moves.get(item["row"].pk)
        if move is None:
            continue
        membership = IdentityMembership.objects.select_for_update(of=("self",)).get(pk=item["row"].pk)
        if membership.reviewed_by_id or membership.state not in SYSTEM_STATES:
            continue                                            # decided by the owner since the plan was made: left alone
        target = resolve(move["to_group"])
        before = {"group_id": membership.identity_group_id, "state": membership.state}
        if membership.identity_group_id:
            touched.add(membership.identity_group_id)
        if target:
            touched.add(target)
        membership.identity_group_id = target
        membership.state = move["to_state"]
        best = item["best"]
        membership.similarity_score = best
        membership.matching_distance = (1 - best) if best is not None else None
        membership.decision_method = METHOD
        membership.evidence = {**(membership.evidence or {}), "regrouped": True, "best_score": best, "second_best_score": item["second"],
                               "threshold": plan["limits"]["match"], "new_group_threshold": plan["limits"]["new_group"], "ambiguity_margin": plan["limits"]["margin"]}
        membership.save()
        IdentityChange.objects.create(
            owner=owner, actor=None, group_id=target, membership=membership, action=IdentityChange.Action.MOVED, before=before,
            after={"group_id": target, "state": membership.state}, reason=REASON,
        )
        moved += 1
    for group in IdentityGroup.objects.filter(pk__in=touched):
        _refresh_group(group)
    merged, archived = [], []
    for entry in plan["merge_or_archive"]:
        source = IdentityGroup.objects.filter(pk=entry["folder"], owner=owner, state=IdentityGroup.State.ACTIVE).first()
        if source is None:
            continue
        target = resolve(entry["into"]) if entry["into"] else None
        if source.memberships.filter(state__in=[*_confirmed_states(), IdentityMembership.State.NEEDS_REVIEW]).exists():
            continue                                            # something still points here: never leave it dangling
        if target and IdentityGroup.objects.filter(pk=target, owner=owner, state=IdentityGroup.State.ACTIVE).exists():
            merge_groups(source, IdentityGroup.objects.get(pk=target), owner, reason=REASON)
            merged.append(source.pk)
        else:
            source.state = IdentityGroup.State.ARCHIVED
            source.save(update_fields=["state", "updated_at"])
            IdentityChange.objects.create(owner=owner, actor=None, group=source, action=IdentityChange.Action.ARCHIVED, before={"state": "active"}, after={"state": "archived"}, reason=REASON)
            archived.append(source.pk)
    record_activity_event(owner, "group", "identities_regrouped", object_type="User", object_id=owner.pk,
                          details={"moved": moved, "created": len(created), "merged": merged, "archived": archived})
    return {"moved": moved, "created_folders": sorted(created.values()), "merged": merged, "archived": archived}

"""Download approval: an organization member downloads an image only after a moderator approved it.

Switched on per organization by the FaceCloud administrator (``Organization.download_approval_required``, off by default). Where it is off, and in every personal
workspace, nothing here applies and downloads behave exactly as before.

The flow: a member selects accepted images and submits ONE request (``create_request``); the organization's owner and the moderators who may see that member are
told; one of them previews the images and decides (``decide``: approve all, approve selected, deny selected, deny all, with a reason for every denial); each
approved item becomes a time-limited grant for that one file of that one image; the member is told once, when every image is decided. Every route that delivers an
image file calls ``authorize_download`` first, so the backend, not a disabled button, is what refuses a download that was not approved.

What a grant covers is explicit: ``enhanced`` (the approved enhanced result; the default), ``original`` (the uploaded file, which the standardized copy needs too) or
``edited`` (one saved edit). A grant for one of them never opens another.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from organizations.models import Organization, OrganizationAuditEvent, OrganizationMembership
from organizations.scope import moderators_who_see
from organizations.services import get_active_membership, record_audit_event

from .models import DownloadGrant, DownloadRequest, DownloadRequestItem

logger = logging.getLogger(__name__)

Request = DownloadRequest
Item = DownloadRequestItem
Grant = DownloadGrant
Artifact = DownloadRequestItem.ArtifactType
Reason = DownloadRequestItem.DenialReason
Action = OrganizationAuditEvent.Action

# What each reason code means, in words a person can read (the API returns the code and this sentence).
REASON_SENTENCES = {
    "not_found": "This image was not found among your photographs of this organization.",
    "not_in_organization": "This image was not submitted to this organization.",
    "not_yours": "You can only request your own images.",
    "not_available": "This image is in the trash or was not accepted for storage.",
    "not_approved": "This image has not been approved yet, so it cannot be requested.",
    "not_processed": "This image has no finished enhancement to download yet.",
    "artifact_unavailable": "The requested file of this image is not available.",
    "already_requested": "This image is already part of a request that has not been decided.",
    "already_granted": "You already have an active approval to download this image.",
    "no_grant": "This image has not been approved for download. Request approval first.",
    "expired": "The approval for this image has expired. Request it again.",
    "revoked": "The approval for this image was withdrawn.",
    "used_up": "The approval for this image has been used as often as it allows.",
    "not_a_member": "You are no longer an active member of this organization.",
    "organization_suspended": "This organization is suspended.",
    "no_longer_eligible": "The image was no longer eligible when the request was decided.",
}


class DownloadNotAuthorized(PermissionDenied):
    """403 ``download_approval_required``: the file is not delivered. ``reason`` says why (see ``REASON_SENTENCES``)."""

    default_code = "download_approval_required"

    def __init__(self, reason):
        super().__init__(detail={"detail": REASON_SENTENCES.get(reason, "This download is not approved."), "code": self.default_code, "reason": reason})


class DownloadConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "download_conflict"
    default_detail = "This conflicts with the request's current state."

    def __init__(self, code, detail):
        super().__init__(detail={"detail": detail, "code": code})


class ItemsNotEligible(APIException):
    """400 with one entry per image that cannot be requested: nothing is saved when any image is refused, so the request is exactly the list that was meant."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "items_not_eligible"

    def __init__(self, entries):
        super().__init__(detail={"detail": "Some of the selected images cannot be requested.", "code": self.default_code})
        self.detail["items"] = entries                  # DRF would turn every value of a detail dict into text; the image ids stay numbers


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Which images are under approval, and what is eligible
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


def organization_requires_approval(organization):
    return bool(organization is not None and not organization.is_personal and organization.download_approval_required)


def approval_organization(image):
    """The organization whose approval ``image`` needs, or ``None`` (a personal workspace, no organization, or approval switched off)."""
    organization = image.batch.organization
    return organization if organization_requires_approval(organization) else None


def batch_requires_approval(batch):
    return organization_requires_approval(batch.organization)


def grant_hours(organization):
    return organization.download_grant_hours or settings.DOWNLOAD_GRANT_DEFAULT_HOURS


def _accepted(image):
    from quality_control.effective import effective_decision

    quality = getattr(image, "quality_decision", None)
    return quality is not None and quality.status == "completed" and effective_decision(image, quality.decision) == "accepted"


def _resolve_artifact(image, artifact_type, artifact_id=None):
    """``(artifact_id, reason)``: the file an item names, or why there is none."""
    from enhancement.models import EnhancementStatus, ImageEditVersion

    if artifact_type == Artifact.ORIGINAL:
        return (image.pk, None) if image.original_file else (None, "artifact_unavailable")
    if artifact_type == Artifact.ENHANCED:
        results = image.enhancement_results.filter(status=EnhancementStatus.COMPLETED).exclude(enhanced_file="")
        result = results.filter(pk=artifact_id).first() if artifact_id else results.order_by("-finished_at", "-pk").first()
        return (result.pk, None) if result else (None, "not_processed")
    if artifact_type == Artifact.EDITED:
        if not artifact_id:
            return None, "artifact_unavailable"
        edit = ImageEditVersion.objects.filter(
            pk=artifact_id, enhancement_result__image=image, status=EnhancementStatus.COMPLETED,
        ).exclude(edited_file="").first()
        return (edit.pk, None) if edit else (None, "artifact_unavailable")
    return None, "artifact_unavailable"


def _active_grant_q(now=None):
    return Q(revoked_at__isnull=True, expires_at__gt=now or timezone.now())


def check_eligibility(user, organization, image, artifact_type=Artifact.ENHANCED, artifact_id=None):
    """``(artifact_id, reason)``: whether ``user`` may put this file of ``image`` into a request now (``reason`` is ``None`` when they may)."""
    if image.batch.organization_id != organization.pk:
        return None, "not_in_organization"
    if image.owner_id != user.pk:
        return None, "not_yours"
    if image.status != "uploaded" or image.trashed_at is not None:
        return None, "not_available"
    if not _accepted(image):
        return None, "not_approved"
    resolved, reason = _resolve_artifact(image, artifact_type, artifact_id)
    if reason:
        return None, reason
    if Grant.objects.filter(_active_grant_q(), grantee=user, image=image, artifact_type=artifact_type, artifact_id=resolved).exists():
        return None, "already_granted"
    if Item.objects.filter(
        request__requester=user, request__status__in=Request.OPEN_STATUSES, image=image, artifact_type=artifact_type, status=Item.Status.PENDING,
    ).exists():
        return None, "already_requested"
    return resolved, None


def access_state(user, image, artifact_type=Artifact.ENHANCED, artifact_id=None):
    """The download-access state of one file for the person: ``not_requested``, ``pending``, ``granted``, ``denied``, ``expired`` or ``revoked``."""
    resolved, _ = _resolve_artifact(image, artifact_type, artifact_id)
    if resolved is None:
        return "not_requested"
    grants = Grant.objects.filter(grantee=user, image=image, artifact_type=artifact_type, artifact_id=resolved).order_by("-created_at", "-pk")
    now = timezone.now()
    latest = grants.first()
    if grants.filter(_active_grant_q(now)).exists():
        return "granted"
    if Item.objects.filter(request__requester=user, request__status__in=Request.OPEN_STATUSES, image=image, artifact_type=artifact_type, status=Item.Status.PENDING).exists():
        return "pending"
    if latest is not None:
        return "revoked" if latest.revoked_at else "expired"
    last_item = Item.objects.filter(request__requester=user, image=image, artifact_type=artifact_type).order_by("-pk").first()
    return "denied" if last_item is not None and last_item.status == Item.Status.DENIED else "not_requested"


def eligibility_report(user, organization, entries):
    """One row per ``{"image_id", "artifact_type"?, "artifact_id"?}``: whether it can be requested, why not, and its download-access state."""
    from uploads.models import ImageUpload

    ids = [entry["image_id"] for entry in entries]
    images = {
        image.pk: image for image in ImageUpload.objects.select_related("batch__organization", "quality_decision").filter(pk__in=ids, owner=user, batch__organization=organization)
    }
    rows = []
    for entry in entries:
        image = images.get(entry["image_id"])
        artifact_type = entry.get("artifact_type") or Artifact.ENHANCED
        if image is None:
            rows.append({"image_id": entry["image_id"], "artifact_type": artifact_type, "eligible": False, "reason": "not_found", "detail": REASON_SENTENCES["not_found"], "access_state": "not_requested"})
            continue
        _, reason = check_eligibility(user, organization, image, artifact_type, entry.get("artifact_id"))
        rows.append({
            "image_id": image.pk, "artifact_type": artifact_type, "eligible": reason is None, "reason": reason or "",
            "detail": REASON_SENTENCES.get(reason, "") if reason else "", "access_state": access_state(user, image, artifact_type, entry.get("artifact_id")),
        })
    return rows


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Who can decide a request
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


MODERATOR_REQUIRED = {"detail": "Only the organization's moderators decide download requests.", "code": "moderator_required"}


def assert_moderator(membership):
    """The approval role is the organization's **moderator**: not the owner (who oversees: reads every request and its history), not a member, not the administrator."""
    if membership is None or membership.status != OrganizationMembership.Status.ACTIVE or membership.role != OrganizationMembership.Role.MODERATOR:
        raise PermissionDenied(detail=MODERATOR_REQUIRED)


def eligible_reviewers(organization, requester_id):
    """The active **moderators** who may decide ``requester_id``'s requests: not the requester (nobody approves their own request, a moderator's included), not the owner, and, when
    moderators see only their own members, only those who may see this member. Suspended or deactivated accounts do not count."""
    return [
        membership for membership in moderators_who_see(organization, requester_id)
        if membership.role == OrganizationMembership.Role.MODERATOR and membership.user_id != requester_id
        and membership.user.is_active and not getattr(membership.user, "is_suspended", False)
    ]


def assert_reviewer_available(organization, requester_id):
    if not eligible_reviewers(organization, requester_id):
        raise DownloadConflict(
            "no_eligible_reviewer",
            "Nobody in your organization can review this request yet: a request is never approved by the person who made it. Ask your organization owner or the FaceCloud administrator to add a moderator.",
        )


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Submitting a request
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


def create_request(*, user, organization_id, entries, message="", client_token="", http_request=None):
    """Save one request for the images in ``entries``; returns ``(request, created)``. ``created`` is ``False`` when ``client_token`` names a request of theirs that exists."""
    from uploads.models import ImageUpload

    membership = get_active_membership(user, organization_id)
    if membership is None:
        raise ValidationError({"organization_id": ["You are not an active member of that organization."]})
    organization = membership.organization
    if organization.status == Organization.Status.SUSPENDED:
        raise DownloadNotAuthorized("organization_suspended")
    if not organization_requires_approval(organization):
        raise DownloadConflict("approval_not_required", "This organization does not require approval: download your images directly.")
    if client_token:
        existing = Request.objects.filter(requester=user, client_token=client_token).first()
        if existing is not None:
            _assert_same_submission(existing, organization, entries)
            return existing, False
    assert_reviewer_available(organization, user.pk)
    if not 1 <= len(entries) <= settings.DOWNLOAD_REQUEST_MAX_ITEMS:
        raise ValidationError({"items": [f"Select between 1 and {settings.DOWNLOAD_REQUEST_MAX_ITEMS} images."]})
    seen = set()
    for entry in entries:
        key = (entry["image_id"], entry.get("artifact_type") or Artifact.ENHANCED)
        if key in seen:
            raise ValidationError({"items": ["The same image and file is listed twice."]})
        seen.add(key)

    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)               # one submission at a time per person: the "already requested" check below cannot race
            if client_token:                                                          # the same submission sent twice at once: the second one finds the first, now it is committed
                existing = Request.objects.filter(requester=user, client_token=client_token).first()
                if existing is not None:
                    _assert_same_submission(existing, organization, entries)
                    return existing, False
            images = {
                image.pk: image for image in
                ImageUpload.objects.select_related("batch__organization", "quality_decision").filter(pk__in=[entry["image_id"] for entry in entries], owner=user, batch__organization=organization)
            }
            refused, resolved_entries = [], []
            for entry in entries:
                artifact_type = entry.get("artifact_type") or Artifact.ENHANCED
                image = images.get(entry["image_id"])
                if image is None:
                    refused.append({"image_id": entry["image_id"], "artifact_type": artifact_type, "reason": "not_found", "detail": REASON_SENTENCES["not_found"]})
                    continue
                artifact_id, reason = check_eligibility(user, organization, image, artifact_type, entry.get("artifact_id"))
                if reason:
                    refused.append({"image_id": image.pk, "artifact_type": artifact_type, "reason": reason, "detail": REASON_SENTENCES.get(reason, "")})
                else:
                    resolved_entries.append((image, artifact_type, artifact_id))
            if refused:
                raise ItemsNotEligible(refused)
            download_request = Request.objects.create(organization=organization, requester=user, requester_message=(message or "").strip()[:500], client_token=client_token or "")
            Item.objects.bulk_create([Item(request=download_request, image=image, artifact_type=kind, artifact_id=artifact_id) for image, kind, artifact_id in resolved_entries])
            record_audit_event(
                organization, user, Action.DOWNLOAD_REQUESTED, target_type="DownloadRequest", target_id=download_request.pk,
                new_value={"images": len(resolved_entries), "image_ids": [image.pk for image, _, _ in resolved_entries]}, request=http_request,
            )
            request_id = download_request.pk
            transaction.on_commit(lambda: _notify_submitted(request_id))
    except IntegrityError:
        existing = Request.objects.filter(requester=user, client_token=client_token).first() if client_token else None
        if existing is None:
            raise
        _assert_same_submission(existing, organization, entries)
        return existing, False
    return download_request, True


def _assert_same_submission(existing, organization, entries):
    """A ``client_token`` stands for ONE submission: reusing it for a different organization, selection or file scope is a 409, never a silent return of the earlier request."""
    saved = {(item.image_id, item.artifact_type): item.artifact_id for item in existing.items.all()}
    sent = {(entry["image_id"], entry.get("artifact_type") or Artifact.ENHANCED): entry.get("artifact_id") for entry in entries}
    same = (
        existing.organization_id == organization.pk and saved.keys() == sent.keys()
        and all(artifact_id is None or artifact_id == saved[key] for key, artifact_id in sent.items())
    )
    if not same:
        raise DownloadConflict("client_token_reused", "This client_token was already used for a different selection of images. Use a new token for a new request.")


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Deciding a request
# --------------------------------------------------------------------------------------------------------------------------------------------------------------

APPROVE_ALL, APPROVE_SELECTED, DENY_SELECTED, DENY_ALL = "approve_all", "approve_selected", "deny_selected", "deny_all"
ACTIONS = (APPROVE_ALL, APPROVE_SELECTED, DENY_SELECTED, DENY_ALL)


def recompute_status(items):
    """The request's status from its items' decisions."""
    statuses = [item.status for item in items]
    pending, approved = Item.Status.PENDING in statuses, Item.Status.APPROVED in statuses
    denied = Item.Status.DENIED in statuses
    if pending:
        return Request.Status.IN_REVIEW if approved or denied else Request.Status.PENDING
    if approved and denied:
        return Request.Status.PARTIALLY_APPROVED
    return Request.Status.APPROVED if approved else Request.Status.DENIED


def _reason_for(item, default, per_item):
    chosen = per_item.get(str(item.pk)) or per_item.get(item.pk) or default
    code = (chosen or {}).get("code") or ""
    text = ((chosen or {}).get("text") or "").strip()
    if code not in Reason.values or code == Reason.NO_LONGER_ELIGIBLE:
        raise ValidationError({"reason_code": [f"Choose a reason for every denied image (one of: {', '.join(v for v in Reason.values if v != Reason.NO_LONGER_ELIGIBLE)})."]})
    if code == Reason.OTHER and not text:
        raise ValidationError({"reason_text": ["Explain the reason when you choose 'other'."]})
    return code, text[:500]


def _still_eligible(item):
    image = item.image
    if image.status != "uploaded" or image.trashed_at is not None or not _accepted(image):
        return False
    resolved, reason = _resolve_artifact(image, item.artifact_type, item.artifact_id)
    return reason is None and resolved == item.artifact_id


def decide(*, download_request_id, actor, organization, scope, action, item_ids=None, reason=None, item_reasons=None, expected_version=None, http_request=None):
    """Record a moderator's decision. ``scope`` is the actor's ``DataScope``; ``reason`` is ``{"code", "text"}`` for the denials, ``item_reasons`` overrides it per item id.

    Everything happens under a lock on the request, so two moderators cannot decide the same image twice: the later one gets 409.
    """
    assert_moderator(scope.membership)               # checked here as well as by the view: the service is the rule, the route only the way in
    if action not in ACTIONS:
        raise ValidationError({"action": [f"Choose one of: {', '.join(ACTIONS)}."]})
    item_ids = [int(value) for value in (item_ids or [])]
    item_reasons = item_reasons or {}
    with transaction.atomic():
        download_request = Request.objects.select_for_update(of=("self",)).select_related("requester").filter(pk=download_request_id, organization=organization).first()
        if download_request is None or not scope.sees(download_request.requester_id):
            from django.http import Http404

            raise Http404("Download request not found.")
        if organization.status == Organization.Status.SUSPENDED:
            raise DownloadNotAuthorized("organization_suspended")
        if download_request.requester_id == actor.pk:
            raise PermissionDenied(detail={"detail": "You cannot decide your own request.", "code": "cannot_decide_own_request"})
        if download_request.status not in Request.OPEN_STATUSES:
            raise DownloadConflict("request_closed", "This request is already decided or was cancelled.")
        if expected_version is not None and expected_version != download_request.version:
            raise DownloadConflict("request_changed", "Someone else changed this request. Reload it and decide again.")
        all_items = list(download_request.items.select_for_update(of=("self",)).select_related("image__batch__organization", "image__quality_decision"))
        by_id = {item.pk: item for item in all_items}
        pending = [item for item in all_items if item.status == Item.Status.PENDING]
        if action in (APPROVE_SELECTED, DENY_SELECTED):
            if not item_ids:
                raise ValidationError({"item_ids": ["Select at least one image."]})
            unknown = [value for value in item_ids if value not in by_id]
            if unknown:
                raise ValidationError({"item_ids": [f"Not part of this request: {unknown}."]})
            decided = [value for value in item_ids if by_id[value].status != Item.Status.PENDING]
            if decided:
                raise DownloadConflict("item_already_decided", f"These images were already decided: {decided}.")
            chosen = [by_id[value] for value in dict.fromkeys(item_ids)]
        if action == APPROVE_ALL:
            to_approve, to_deny = pending, []
        elif action == APPROVE_SELECTED:
            to_approve, to_deny = chosen, [item for item in pending if item not in chosen]
        elif action == DENY_SELECTED:
            to_approve, to_deny = [], chosen
        else:
            to_approve, to_deny = [], pending
        denial = {}
        for item in to_deny:
            denial[item.pk] = _reason_for(item, reason, item_reasons)
        # A photograph that stopped being eligible since the request (rejected, trashed, its file gone) is never approved: it is denied with that reason.
        stale = [item for item in to_approve if not _still_eligible(item)]
        to_approve = [item for item in to_approve if item not in stale]
        for item in stale:
            denial[item.pk] = (Reason.NO_LONGER_ELIGIBLE, REASON_SENTENCES["no_longer_eligible"])
        to_deny = to_deny + stale
        now = timezone.now()
        expires_at = now + timedelta(hours=grant_hours(organization))
        max_uses = settings.DOWNLOAD_GRANT_MAX_USES or None
        for item in to_approve:
            item.status, item.reviewed_by, item.reviewed_at = Item.Status.APPROVED, actor, now
            item.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            Grant.objects.create(
                item=item, organization=organization, grantee=download_request.requester, image=item.image, artifact_type=item.artifact_type, artifact_id=item.artifact_id,
                granted_by=actor, expires_at=expires_at, max_uses=max_uses,
            )
        for item in to_deny:
            code, text = denial[item.pk]
            item.status, item.reviewed_by, item.reviewed_at, item.denial_reason_code, item.denial_reason_text = Item.Status.DENIED, actor, now, code, text
            item.save(update_fields=["status", "reviewed_by", "reviewed_at", "denial_reason_code", "denial_reason_text"])
        download_request.version += 1
        download_request.status = recompute_status(all_items)
        finalized = download_request.status not in Request.OPEN_STATUSES
        if finalized:
            download_request.decided_at, download_request.decided_by = now, actor
        download_request.save(update_fields=["version", "status", "decided_at", "decided_by", "updated_at"])
        record_audit_event(
            organization, actor, Action.DOWNLOAD_DECIDED, target_type="DownloadRequest", target_id=download_request.pk,
            new_value={
                "action": action, "status": download_request.status, "approved": [item.pk for item in to_approve],
                "denied": [{"item": item.pk, "reason": denial[item.pk][0]} for item in to_deny], "grants_expire_at": expires_at.isoformat() if to_approve else "",
            },
            request=http_request,
        )
        if finalized:
            request_id = download_request.pk
            transaction.on_commit(lambda: _notify_decided(request_id))
    return download_request


def cancel_request(*, download_request, user, http_request=None):
    """The requester withdraws a request nobody has started deciding."""
    with transaction.atomic():
        locked = Request.objects.select_for_update(of=("self",)).get(pk=download_request.pk, requester=user)
        if locked.status != Request.Status.PENDING:
            raise DownloadConflict("request_not_cancellable", "Only a request that nobody has started to decide can be cancelled.")
        locked.status = Request.Status.CANCELLED
        locked.version += 1
        locked.save(update_fields=["status", "version", "updated_at"])
        record_audit_event(locked.organization, user, Action.DOWNLOAD_REQUEST_CANCELLED, target_type="DownloadRequest", target_id=locked.pk, request=http_request)
    return locked


def revoke_grant(*, grant, actor, membership=None, reason="", http_request=None):
    """Withdraw an approval before it expires. Files already downloaded cannot be recalled; nothing further is delivered. A moderator's act, like a decision."""
    assert_moderator(membership)
    with transaction.atomic():
        locked = Grant.objects.select_for_update().get(pk=grant.pk)
        if locked.revoked_at is None:
            locked.revoked_at, locked.revoked_by, locked.revoke_reason = timezone.now(), actor, (reason or "").strip()[:500]
            locked.save(update_fields=["revoked_at", "revoked_by", "revoke_reason"])
            record_audit_event(
                locked.organization, actor, Action.DOWNLOAD_GRANT_REVOKED, target_type="DownloadGrant", target_id=locked.pk,
                new_value={"image_id": locked.image_id, "artifact_type": locked.artifact_type}, reason=locked.revoke_reason, request=http_request,
            )
    return locked


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Delivering a file
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


def _deny(organization, user, image, artifact_type, reason, http_request):
    try:
        record_audit_event(
            organization, user, Action.DOWNLOAD_BLOCKED, target_type="image", target_id=image.pk,
            new_value={"artifact_type": artifact_type, "reason": reason}, request=http_request,
        )
    except Exception:                                   # a failing log must not turn a refusal into a server error
        logger.exception("Could not record a blocked download image=%s.", image.pk)
    raise DownloadNotAuthorized(reason)


def authorize_download(user, image, artifact_type, artifact_id=None, *, http_request=None, record=True):
    """Return normally when ``user`` may take this file of ``image`` now; raise ``DownloadNotAuthorized`` otherwise. Counts one use of the grant.

    Does nothing for an image whose organization does not require approval, so every existing download behaves as before. ``artifact_id`` defaults to what the
    type means for this image (the image itself for ``original``).
    """
    organization = approval_organization(image)
    if organization is None:
        return None
    artifact_id = image.pk if artifact_type == Artifact.ORIGINAL else artifact_id
    now = timezone.now()
    with transaction.atomic():
        grants = Grant.objects.select_for_update().filter(grantee=user, image=image, artifact_type=artifact_type, artifact_id=artifact_id).order_by("-expires_at", "-pk")
        grant = next((candidate for candidate in grants if candidate.revoked_at is None and candidate.expires_at > now), None)
        if grant is None:
            latest = grants.first()
            reason = "no_grant" if latest is None else ("revoked" if latest.revoked_at else "expired")
        elif organization.status == Organization.Status.SUSPENDED:
            reason = "organization_suspended"
        elif not OrganizationMembership.objects.filter(organization=organization, user=user, status=OrganizationMembership.Status.ACTIVE).exists():
            reason = "not_a_member"
        elif image.status != "uploaded" or image.trashed_at is not None or not _accepted(image):
            reason = "not_approved"
        elif grant.max_uses is not None and grant.use_count >= grant.max_uses:
            reason = "used_up"
        else:
            Grant.objects.filter(pk=grant.pk).update(use_count=F("use_count") + 1)
            if record:
                record_audit_event(
                    organization, user, Action.DOWNLOAD_COMPLETED, target_type="image", target_id=image.pk,
                    new_value={"artifact_type": artifact_type, "grant": grant.pk}, request=http_request,
                )
            return grant
    _deny(organization, user, image, artifact_type, reason, http_request)


def approval_organization_of(images):
    """The first organization that requires approval among the images of the queryset, or ``None`` (for the routes that bundle many images)."""
    return Organization.objects.filter(
        pk__in=images.values("batch__organization_id"), is_personal=False, download_approval_required=True,
    ).order_by("pk").first()


def block_bulk_download(user, organization, what, http_request=None):
    """The refusal for a route that would bundle or share images of an organization that requires approval without a grant per image."""
    try:
        record_audit_event(organization, user, Action.DOWNLOAD_BLOCKED, target_type=what, new_value={"reason": "approval_required"}, request=http_request)
    except Exception:
        logger.exception("Could not record a blocked bulk download.")
    raise PermissionDenied(detail={
        "detail": "This organization requires approval for downloads. Request the images you need and download them once a moderator approves them.",
        "code": "download_approval_required", "reason": "bulk_not_allowed",
    })


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Notifications (created after the decision is saved; a failure here never undoes it)
# --------------------------------------------------------------------------------------------------------------------------------------------------------------

REQUESTS_PATH = "/dashboard/download-requests"
MODERATOR_REQUESTS_PATH = "/moderator/download-requests"


def _notify_submitted(request_id):
    """The requester is told it went in; every reviewer who can decide it gets an in-app task (always, whatever their e-mail or review-update preferences) and an e-mail
    where their settings allow. Each person is handled on their own: a failure for one never stops the others, and none of it touches the request, which stays in the queue."""
    from notifications.models import UserNotification
    from notifications.services import create_user_notification

    try:
        download_request = Request.objects.select_related("requester", "organization").get(pk=request_id)
        count = download_request.items.count()
        who = download_request.requester.get_username()
        base = {"resource_type": "download_request", "resource_id": request_id, "metadata": {"request_id": request_id, "organization_id": download_request.organization_id, "total": count}}
    except Exception:
        logger.exception("Could not read download request %s to notify about it.", request_id)
        return
    try:
        create_user_notification(
            recipient=download_request.requester, event_type=UserNotification.EventType.DOWNLOAD_REQUEST_SUBMITTED,
            idempotency_key=f"download_request:{request_id}:submitted", title="Download Request Submitted",
            message="Your download request has been sent to your organization's moderator for review.", action_path=f"{REQUESTS_PATH}/{request_id}", **base,
        )
    except Exception:
        logger.exception("Could not notify the requester of download request %s.", request_id)
    try:
        reviewers = [membership.user for membership in eligible_reviewers(download_request.organization, download_request.requester_id)]
    except Exception:
        logger.exception("Could not work out who reviews download request %s.", request_id)
        reviewers = []
    if not reviewers:                                   # the reviewers left after the request was made: the platform administrators are told that the organization needs one
        reviewers = list(get_user_model().objects.filter(role="admin", is_active=True)[:20])
        logger.warning("Download request %s has no reviewer who can decide it; the administrators were told.", request_id)
    failed = 0
    for reviewer in reviewers:
        try:
            create_user_notification(
                recipient=reviewer, event_type=UserNotification.EventType.DOWNLOAD_REQUEST_RECEIVED,
                idempotency_key=f"download_request:{request_id}:received", title="Download request to review",
                message=f"{who} asked to download {count} image{'s' if count != 1 else ''} from {download_request.organization.name}.",
                action_path=f"{MODERATOR_REQUESTS_PATH}/{request_id}?organization={download_request.organization_id}", force_in_app=True, **base,
            )
        except Exception:
            failed += 1
            logger.exception("Could not notify reviewer %s of download request %s.", reviewer.pk, request_id)
    if failed:
        logger.error("Download request %s: %s of %s reviewers could not be notified; it is still in the moderator queue.", request_id, failed, len(reviewers))


def _notify_decided(request_id):
    from notifications.models import UserNotification
    from notifications.services import create_user_notification

    try:
        download_request = Request.objects.select_related("requester").get(pk=request_id)
        items = list(download_request.items.all())
        total = len(items)
        approved = sum(1 for item in items if item.status == Item.Status.APPROVED)
        denied = total - approved
        if download_request.status == Request.Status.APPROVED:
            title = "Download Request Approved"
            message = "Your download request has been approved. The authorized images are available in your FaceCloud account until the grant expiration time."
            severity = UserNotification.Severity.SUCCESS
        elif download_request.status == Request.Status.PARTIALLY_APPROVED:
            title = "Download Request Partially Approved"
            message = f"Your request has been approved for {approved} of {total} selected images. {denied} image{'s were' if denied != 1 else ' was'} denied. Open your request to review the decisions."
            severity = UserNotification.Severity.WARNING
        else:
            title = "Download Request Denied"
            message = "Your request was denied. Open FaceCloud to review the moderator's reasons."
            severity = UserNotification.Severity.WARNING
        create_user_notification(
            recipient=download_request.requester, event_type=UserNotification.EventType.DOWNLOAD_REQUEST_DECIDED,
            idempotency_key=f"download_request:{request_id}:decided", title=title, message=message, severity=severity,
            resource_type="download_request", resource_id=request_id, action_path=f"{REQUESTS_PATH}/{request_id}",
            metadata={"request_id": request_id, "status": download_request.status, "total": total, "approved": approved, "denied": denied},
        )
    except Exception:
        logger.exception("Could not create the notification of the decision on download request %s.", request_id)

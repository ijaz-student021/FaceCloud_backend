"""A member's request to leave an organization, and the moderator's decision on it.

The member never suspends themselves. ``request_leave`` records the request; a moderator who may see that member (or, as an audited emergency override, a FaceCloud
administrator) approves or denies it. Approving suspends **that one membership** (the account, the personal workspace and every other membership are untouched);
organization content stays stored and keeps its organization policy, and the person's organization-specific download grants end. Every transition runs under a row
lock, so an approval and a cancellation at the same instant cannot both win. Seats: a pending request still holds its seat (the member is still active); an approved
one releases it (only ACTIVE memberships count), and reactivating later needs a free seat (``update_member`` checks).
"""
import logging

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from .models import OrganizationAuditEvent, OrganizationLeaveRequest, OrganizationMembership
from .services import assert_organization_active, assert_shared_organization, record_audit_event

logger = logging.getLogger(__name__)
Status = OrganizationLeaveRequest.Status
Action = OrganizationAuditEvent.Action
REASON_MAX = 500
MODERATOR_PATH = "/moderator/leave-requests"
MEMBER_PATH = "/dashboard/organization"


class LeaveConflict(APIException):
    status_code = 409

    def __init__(self, code, message):
        super().__init__(detail={"detail": message, "code": code})


def _clean(text, *, field):
    text = (text or "").strip()
    if len(text) > REASON_MAX:
        raise ValidationError({field: f"Keep it to {REASON_MAX} characters."})
    return text


def reviewers_of(organization, user_id):
    """The active moderators who may decide this member's request (never the member; and, when moderators see only their own members, only those who see this one)."""
    from downloads.approval import eligible_reviewers

    return eligible_reviewers(organization, user_id)


def _assert_moderator(membership):
    if membership is None or membership.status != OrganizationMembership.Status.ACTIVE or membership.role != OrganizationMembership.Role.MODERATOR:
        raise PermissionDenied(detail={"detail": "Only the organization's moderators decide leave requests.", "code": "moderator_required"})


def current_request(user, organization):
    """The member's pending request in ``organization`` or, failing that, the latest one (``None`` when there never was one)."""
    queryset = OrganizationLeaveRequest.objects.filter(user=user, organization=organization)
    return queryset.filter(status=Status.PENDING).first() or queryset.first()


@transaction.atomic
def request_leave(*, user, organization, reason="", http_request=None):
    reason = _clean(reason, field="reason")
    membership = (
        OrganizationMembership.objects.select_for_update(of=("self",)).select_related("organization")
        .filter(organization=organization, user=user, status=OrganizationMembership.Status.ACTIVE).first()
    )
    if membership is None:
        raise Http404("Organization not found.")
    organization = membership.organization
    assert_shared_organization(organization)                 # a personal workspace is not left, it is the person's own
    assert_organization_active(organization)
    if membership.role != OrganizationMembership.Role.MEMBER:
        raise PermissionDenied(detail={
            "detail": "Only a member can ask to leave. An owner or moderator is moved by the organization owner or the FaceCloud administrator.",
            "code": "leave_not_available",
        })
    if OrganizationLeaveRequest.objects.filter(membership=membership, status=Status.PENDING).exists():
        raise LeaveConflict("leave_request_pending", "You already have a leave request waiting for a moderator.")
    if not reviewers_of(organization, user.pk):
        raise LeaveConflict(
            "no_eligible_reviewer",
            "Nobody in your organization can review this yet: a request is never approved by the person who made it. Ask your organization owner or the FaceCloud administrator to add a moderator.",
        )
    leave = OrganizationLeaveRequest.objects.create(organization=organization, membership=membership, user=user, reason=reason)
    record_audit_event(
        organization, user, Action.LEAVE_REQUESTED, target_type="OrganizationLeaveRequest", target_id=leave.pk,
        new_value={"status": leave.status, "membership_id": membership.pk}, reason=reason, request=http_request,
    )
    leave_id = leave.pk
    transaction.on_commit(lambda: _notify_submitted(leave_id))
    return leave


@transaction.atomic
def cancel_leave(*, user, organization, leave_id=None, http_request=None):
    queryset = OrganizationLeaveRequest.objects.select_for_update(of=("self",)).filter(user=user, organization=organization)
    # The latest request is locked whatever its state: a cancellation that loses the race to an approval then reads the decision (409), not "nothing to cancel" (404).
    leave = queryset.filter(pk=leave_id).first() if leave_id else queryset.order_by("-submitted_at", "-pk").first()
    if leave is None:
        raise Http404("Leave request not found.")
    if leave.status != Status.PENDING:
        raise LeaveConflict("leave_request_not_pending", f"This request was already {leave.get_status_display().lower()}; it can no longer be cancelled.")
    leave.status, leave.decided_at = Status.CANCELLED, timezone.now()
    leave.save(update_fields=["status", "decided_at", "updated_at"])
    record_audit_event(
        organization, user, Action.LEAVE_REQUEST_CANCELLED, target_type="OrganizationLeaveRequest", target_id=leave.pk,
        previous_value={"status": Status.PENDING}, new_value={"status": leave.status}, request=http_request,
    )
    leave_id = leave.pk
    transaction.on_commit(lambda: _notify_member(leave_id, "cancelled"))
    return leave


def _end_download_access(user, organization, actor, http_request):
    """The person's organization-specific download rights end with the suspension: open requests are cancelled, live grants revoked. Files already downloaded cannot be recalled."""
    from downloads.models import DownloadGrant, DownloadRequest

    now = timezone.now()
    cancelled = DownloadRequest.objects.filter(organization=organization, requester=user, status=DownloadRequest.Status.PENDING).update(status=DownloadRequest.Status.CANCELLED, updated_at=now)
    revoked = DownloadGrant.objects.filter(organization=organization, grantee=user, revoked_at__isnull=True, expires_at__gt=now).update(
        revoked_at=now, revoked_by=actor, revoke_reason="The member's organization access was suspended.")
    return cancelled, revoked


@transaction.atomic
def decide_leave(*, leave, actor, actor_membership, action, reason="", admin_override=False, http_request=None):
    """Approve (suspends the membership) or deny. ``actor_membership`` is the moderator's membership; ``admin_override`` is the FaceCloud administrator's audited emergency path."""
    if action not in ("approve", "deny"):
        raise ValidationError({"action": "Choose approve or deny."})
    reason = _clean(reason, field="reason")
    if action == "deny" and len(reason) < 3:
        raise ValidationError({"reason": "Tell the member why the request is denied."})
    if admin_override and len(reason) < 8:
        raise ValidationError({"reason": "An administrator's decision needs a reason of at least 8 characters (it is kept in the audit trail)."})
    locked = OrganizationLeaveRequest.objects.select_for_update(of=("self",)).select_related("organization", "user").get(pk=leave.pk)
    organization = locked.organization
    if not admin_override:
        _assert_moderator(actor_membership)
        if actor_membership.organization_id != organization.pk or not any(m.pk == actor_membership.pk for m in reviewers_of(organization, locked.user_id)):
            raise Http404("Leave request not found.")           # another organization's, or a member this moderator may not see: the same 404 as an id that does not exist
    if locked.status != Status.PENDING:
        raise LeaveConflict("leave_request_not_pending", f"This request was already {locked.get_status_display().lower()}.")
    locked.reviewed_by, locked.decided_at, locked.decision_reason, locked.admin_override = actor, timezone.now(), reason, admin_override
    detail = {"admin_override": admin_override}
    if action == "approve":
        membership = OrganizationMembership.objects.select_for_update(of=("self",)).get(pk=locked.membership_id)
        previous_status = membership.status
        if previous_status != OrganizationMembership.Status.SUSPENDED:
            membership.status = OrganizationMembership.Status.SUSPENDED
            membership.save(update_fields=["status", "updated_at"])
            record_audit_event(
                organization, actor, Action.MEMBER_STATUS_CHANGED, target_type="OrganizationMembership", target_id=membership.pk,
                previous_value={"role": membership.role, "status": previous_status}, new_value={"role": membership.role, "status": membership.status, "leave_request": locked.pk},
                reason="The member's leave request was approved.", request=http_request,
            )
        cancelled, revoked = _end_download_access(locked.user, organization, actor, http_request)
        detail.update({"membership_status": membership.status, "download_requests_cancelled": cancelled, "download_grants_revoked": revoked})
        locked.status = Status.APPROVED
    else:
        locked.status = Status.DENIED
    locked.save(update_fields=["status", "reviewed_by", "decided_at", "decision_reason", "admin_override", "updated_at"])
    record_audit_event(
        organization, actor, Action.LEAVE_REQUEST_APPROVED if action == "approve" else Action.LEAVE_REQUEST_DENIED,
        target_type="OrganizationLeaveRequest", target_id=locked.pk, previous_value={"status": Status.PENDING}, new_value={"status": locked.status, **detail},
        reason=reason, request=http_request,
    )
    leave_id = locked.pk
    transaction.on_commit(lambda: _notify_member(leave_id, "approved" if action == "approve" else "denied"))
    return locked


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# Notifications (never raise: a failed notification must not undo a decision)
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


def _notify_submitted(leave_id):
    from notifications.models import UserNotification
    from notifications.services import create_user_notification

    Event = UserNotification.EventType
    try:
        leave = OrganizationLeaveRequest.objects.select_related("organization", "user").get(pk=leave_id)
        name = leave.user.full_name or leave.user.username
        for reviewer in reviewers_of(leave.organization, leave.user_id):
            create_user_notification(
                recipient=reviewer.user, event_type=Event.LEAVE_REQUEST_SUBMITTED, idempotency_key=f"leave_request:{leave_id}:submitted:{reviewer.user_id}",
                title="Membership leave request", message=f"{name} asked to leave {leave.organization.name}. Review it in the moderator portal.",
                severity=UserNotification.Severity.INFO, resource_type="leave_request", resource_id=leave_id, action_path=MODERATOR_PATH,
                metadata={"leave_request_id": leave_id, "organization_id": leave.organization_id}, force_in_app=True,
            )
        create_user_notification(
            recipient=leave.user, event_type=Event.LEAVE_REQUEST_RECEIVED, idempotency_key=f"leave_request:{leave_id}:received",
            title="Leave request received", message=f"Your request to leave {leave.organization.name} is waiting for a moderator. Your access stays as it is until it is approved.",
            severity=UserNotification.Severity.INFO, resource_type="leave_request", resource_id=leave_id, action_path=MEMBER_PATH,
            metadata={"leave_request_id": leave_id, "organization_id": leave.organization_id},
        )
    except Exception:
        logger.exception("Could not create the notifications of leave request %s.", leave_id)


_MEMBER_TEXT = {
    "approved": ("LEAVE_REQUEST_APPROVED", "Leave request approved", "Your request to leave {org} was approved. Your access to that organization is suspended; your personal FaceCloud account is not affected.", "SUCCESS"),
    "denied": ("LEAVE_REQUEST_DENIED", "Leave request denied", "Your request to leave {org} was denied. Open FaceCloud to read the moderator's reason.", "WARNING"),
    "cancelled": ("LEAVE_REQUEST_CANCELLED", "Leave request cancelled", "You cancelled your request to leave {org}.", "INFO"),
}


def _notify_member(leave_id, outcome):
    from notifications.models import UserNotification
    from notifications.services import create_user_notification

    event_name, title, message, severity = _MEMBER_TEXT[outcome]
    try:
        leave = OrganizationLeaveRequest.objects.select_related("organization", "user").get(pk=leave_id)
        create_user_notification(
            recipient=leave.user, event_type=getattr(UserNotification.EventType, event_name), idempotency_key=f"leave_request:{leave_id}:{outcome}",
            title=title, message=message.format(org=leave.organization.name), severity=getattr(UserNotification.Severity, severity),
            resource_type="leave_request", resource_id=leave_id, action_path=MEMBER_PATH,
            metadata={"leave_request_id": leave_id, "organization_id": leave.organization_id, "status": leave.status}, force_in_app=True,
        )
    except Exception:
        logger.exception("Could not create the %s notification of leave request %s.", outcome, leave_id)

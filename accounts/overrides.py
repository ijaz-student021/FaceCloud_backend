"""An administrator putting somebody into an organization as a moderator: an exceptional operation, so it must say why and is audited in the organization's own trail.

The ordinary way is the organization's: the Platform Admin creates the organization, its owner invites moderators, moderators invite members. The administrator keeps three ways
around the owner (create a moderator account, send an account invitation for one, promote an account), and each of them needs a ``reason`` and writes an
``admin_moderator_override`` event the organization's owner can read. None of them bypasses the plan's moderator seats, an organization that still waits for its owner (it is not active,
so it cannot be chosen), or a personal workspace (it cannot be chosen either): those checks run before and after.
"""
from rest_framework.exceptions import ValidationError

from organizations.models import OrganizationAuditEvent
from organizations.services import record_audit_event

MIN_REASON_LENGTH = 8


def clean_override_reason(reason):
    """The reason, trimmed; ``ValidationError`` on the field ``reason`` when it is missing or too short to mean anything."""
    reason = " ".join((reason or "").split())
    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError({"reason": [
            f"Say why an administrator is adding this moderator instead of the organization's owner inviting them (at least {MIN_REASON_LENGTH} characters). "
            "The reason is kept in the organization's audit log."
        ]})
    return reason[:500]


def record_moderator_override(*, actor, organization, operation, target_type, target_id, subject, reason, request=None):
    """Write the ``admin_moderator_override`` event: who, which organization, which operation (create_account, account_invitation, promotion), for whom, and why."""
    record_audit_event(
        organization, actor, OrganizationAuditEvent.Action.ADMIN_MODERATOR_OVERRIDE, target_type=target_type, target_id=target_id,
        new_value={"operation": operation, "subject": subject, "role": "moderator"}, reason=reason, request=request,
    )

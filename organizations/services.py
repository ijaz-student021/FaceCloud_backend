"""Organization, membership, invitation and audit logic (Feature 46 / T-008).

Every user gets exactly one organization they own, created alongside their
account (``create_personal_organization``, called from accounts' registration
flow) or backfilled for pre-existing accounts by
``0002_backfill_personal_organizations``. Org-scoping is additive: a lone
user's personal organization behaves exactly like today's user-only ownership
until someone is actually invited in.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.services import get_client_ip

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership

INVITABLE_ROLES = {OrganizationInvitation.Role.MODERATOR, OrganizationInvitation.Role.MEMBER}
MANAGER_ROLES = {OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR}
PROTECTED_FROM_MODERATOR = {OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR}


class OrganizationConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "organization_conflict"
    default_detail = "This action conflicts with the organization's current state."


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def record_audit_event(
    organization, actor, action, *, target_type="", target_id=None,
    previous_value=None, new_value=None, reason="", request=None,
):
    OrganizationAuditEvent.objects.create(
        organization=organization, actor=actor, action=action,
        target_type=target_type, target_id=target_id,
        previous_value=previous_value or {}, new_value=new_value or {},
        reason=reason, ip_address=get_client_ip(request) if request is not None else None,
    )


def _unique_slug(base):
    candidate = slugify(base)[:50] or "org"
    slug = candidate
    suffix = 1
    while Organization.objects.filter(slug=slug).exists():
        suffix += 1
        slug = f"{candidate}-{suffix}"
    return slug


@transaction.atomic
def create_organization(*, name, owner, plan=Organization.Plan.FREE, is_personal=False, actor=None, request=None):
    organization = Organization.objects.create(
        name=name, slug=_unique_slug(name), plan=plan, is_personal=is_personal,
    )
    OrganizationMembership.objects.create(
        organization=organization, user=owner, role=OrganizationMembership.Role.OWNER,
        status=OrganizationMembership.Status.ACTIVE,
    )
    record_audit_event(
        organization, actor or owner, OrganizationAuditEvent.Action.ORGANIZATION_CREATED,
        target_type="Organization", target_id=organization.pk,
        new_value={"name": name, "owner": owner.pk}, request=request,
    )
    return organization


def create_personal_organization(user):
    return create_organization(
        name=f"{user.username}'s workspace", owner=user,
        plan=Organization.Plan.FREE, is_personal=True, actor=user,
    )


def get_default_organization(user):
    """The organization new content the user creates should be tagged with.

    Prefers their personal organization (every user has exactly one, from
    ``create_personal_organization``) so ownership keeps behaving the way it
    always has; falls back to their oldest other active membership, and to
    ``None`` only for a user with no organization at all (a test fixture
    created directly rather than through registration).
    """
    membership = (
        OrganizationMembership.objects.select_related("organization")
        .filter(user=user, status=OrganizationMembership.Status.ACTIVE)
        .order_by("-organization__is_personal", "created_at")
        .first()
    )
    return membership.organization if membership else None


def get_active_membership(user, organization_id):
    if not organization_id:
        return None
    return (
        OrganizationMembership.objects.select_related("organization")
        .filter(
            organization_id=organization_id, user=user,
            status=OrganizationMembership.Status.ACTIVE,
        )
        .first()
    )


def _send_invitation_email(invitation, token):
    link = f"{settings.FRONTEND_URL.rstrip('/')}/invitations/{token}"
    send_mail(
        subject=f"You've been invited to join {invitation.organization.name} on FaceCloud",
        message=(
            f"You've been invited to join {invitation.organization.name} as a "
            f"{invitation.get_role_display().lower()}.\n\n"
            f"Accept the invitation: {link}\n\n"
            f"This invitation expires in {settings.ORG_INVITATION_TTL_HOURS} hours. "
            "If you weren't expecting this, you can ignore this email."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[invitation.email], fail_silently=False,
    )


def invite_member(*, organization, actor_membership, email, role, request=None):
    if role not in INVITABLE_ROLES:
        raise ValidationError({"role": "Invitations may only grant the moderator or member role."})
    if actor_membership.role == OrganizationMembership.Role.MODERATOR and role == OrganizationInvitation.Role.MODERATOR:
        raise PermissionDenied("Moderators may only invite normal members.")
    email = email.strip().lower()
    if OrganizationMembership.objects.filter(
        organization=organization, user__email__iexact=email,
        status=OrganizationMembership.Status.ACTIVE,
    ).exists():
        raise OrganizationConflict("This person is already a member of the organization.")
    if OrganizationInvitation.objects.filter(
        organization=organization, email=email, status=OrganizationInvitation.Status.PENDING,
    ).exists():
        raise OrganizationConflict("An invitation is already pending for this email address.")
    token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation.objects.create(
        organization=organization, email=email, role=role,
        token_hash=hash_token(token), invited_by=actor_membership.user,
        expires_at=timezone.now() + timedelta(hours=settings.ORG_INVITATION_TTL_HOURS),
    )
    _send_invitation_email(invitation, token)
    record_audit_event(
        organization, actor_membership.user, OrganizationAuditEvent.Action.MEMBER_INVITED,
        target_type="OrganizationInvitation", target_id=invitation.pk,
        new_value={"email": email, "role": role}, request=request,
    )
    return invitation


def resend_invitation(*, invitation, actor_membership, request=None):
    if invitation.status != OrganizationInvitation.Status.PENDING:
        raise OrganizationConflict("Only a pending invitation can be resent.")
    token = secrets.token_urlsafe(32)
    invitation.token_hash = hash_token(token)
    invitation.expires_at = timezone.now() + timedelta(hours=settings.ORG_INVITATION_TTL_HOURS)
    invitation.save(update_fields=["token_hash", "expires_at", "updated_at"])
    _send_invitation_email(invitation, token)
    record_audit_event(
        invitation.organization, actor_membership.user, OrganizationAuditEvent.Action.INVITATION_RESENT,
        target_type="OrganizationInvitation", target_id=invitation.pk, request=request,
    )
    return invitation


def cancel_invitation(*, invitation, actor_membership, request=None):
    if invitation.status != OrganizationInvitation.Status.PENDING:
        raise OrganizationConflict("Only a pending invitation can be cancelled.")
    invitation.status = OrganizationInvitation.Status.CANCELLED
    invitation.save(update_fields=["status", "updated_at"])
    record_audit_event(
        invitation.organization, actor_membership.user, OrganizationAuditEvent.Action.INVITATION_CANCELLED,
        target_type="OrganizationInvitation", target_id=invitation.pk, request=request,
    )
    return invitation


@transaction.atomic
def accept_invitation(*, token, user, request=None):
    invitation = (
        OrganizationInvitation.objects.select_for_update()
        .select_related("organization")
        .filter(token_hash=hash_token(token or ""))
        .first()
    )
    if invitation is None:
        raise NotFound("This invitation does not exist.")
    if invitation.status != OrganizationInvitation.Status.PENDING or invitation.is_expired():
        raise OrganizationConflict("This invitation is no longer valid.")
    if invitation.email.lower() != (user.email or "").lower():
        raise PermissionDenied("Sign in with the invited email address to accept this invitation.")
    membership, _ = OrganizationMembership.objects.update_or_create(
        organization=invitation.organization, user=user,
        defaults={
            "role": invitation.role, "status": OrganizationMembership.Status.ACTIVE,
            "invited_by": invitation.invited_by,
        },
    )
    invitation.status = OrganizationInvitation.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["status", "accepted_at", "updated_at"])
    record_audit_event(
        invitation.organization, user, OrganizationAuditEvent.Action.INVITATION_ACCEPTED,
        target_type="OrganizationMembership", target_id=membership.pk,
        new_value={"role": membership.role}, request=request,
    )
    return membership


def _count_active_owners(organization):
    return OrganizationMembership.objects.filter(
        organization=organization, role=OrganizationMembership.Role.OWNER,
        status=OrganizationMembership.Status.ACTIVE,
    ).count()


def _assert_can_manage(actor_membership, target):
    if actor_membership.role == OrganizationMembership.Role.MODERATOR and target.role in PROTECTED_FROM_MODERATOR:
        raise PermissionDenied("Moderators cannot manage an owner or another moderator.")


@transaction.atomic
def update_member(*, membership, actor_membership, role=None, status_value=None, reason="", request=None):
    locked = OrganizationMembership.objects.select_for_update().get(pk=membership.pk)
    _assert_can_manage(actor_membership, locked)
    if role == OrganizationMembership.Role.OWNER and actor_membership.role != OrganizationMembership.Role.OWNER:
        raise PermissionDenied("Only an organization owner can grant the owner role.")
    previous = {"role": locked.role, "status": locked.status}
    demoting_last_owner = (
        locked.role == OrganizationMembership.Role.OWNER
        and _count_active_owners(locked.organization) <= 1
    )
    if role is not None and role != locked.role:
        if demoting_last_owner:
            raise OrganizationConflict("The last organization owner cannot be demoted.")
        locked.role = role
    if status_value is not None and status_value != locked.status:
        if demoting_last_owner and status_value != OrganizationMembership.Status.ACTIVE:
            raise OrganizationConflict("The last organization owner cannot be suspended or removed.")
        locked.status = status_value
    locked.save(update_fields=["role", "status", "updated_at"])
    action = (
        OrganizationAuditEvent.Action.MEMBER_ROLE_CHANGED if role is not None
        else OrganizationAuditEvent.Action.MEMBER_STATUS_CHANGED
    )
    record_audit_event(
        locked.organization, actor_membership.user, action,
        target_type="OrganizationMembership", target_id=locked.pk,
        previous_value=previous, new_value={"role": locked.role, "status": locked.status},
        reason=reason, request=request,
    )
    return locked


@transaction.atomic
def remove_member(*, membership, actor_membership, reason="", request=None):
    locked = OrganizationMembership.objects.select_for_update().get(pk=membership.pk)
    _assert_can_manage(actor_membership, locked)
    if locked.status == OrganizationMembership.Status.REMOVED:
        raise OrganizationConflict("This member has already been removed.")
    if (
        locked.role == OrganizationMembership.Role.OWNER
        and _count_active_owners(locked.organization) <= 1
    ):
        raise OrganizationConflict("The last organization owner cannot be removed.")
    previous = {"role": locked.role, "status": locked.status}
    locked.status = OrganizationMembership.Status.REMOVED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        locked.organization, actor_membership.user, OrganizationAuditEvent.Action.MEMBER_REMOVED,
        target_type="OrganizationMembership", target_id=locked.pk,
        previous_value=previous, new_value={"status": locked.status},
        reason=reason, request=request,
    )
    return locked

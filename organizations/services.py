"""Organization, membership, invitation and audit logic (Feature 46 / T-008).

Every user gets exactly one organization they own, created alongside their
account (``create_personal_organization``, called from accounts' registration
flow) or backfilled for pre-existing accounts by
``0002_backfill_personal_organizations``. Org-scoping is additive: a lone
user's personal organization behaves exactly like today's user-only ownership
until someone is actually invited in.
"""
import hashlib
import logging
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.services import get_client_ip

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership

logger = logging.getLogger(__name__)

INVITABLE_ROLES = {OrganizationInvitation.Role.MODERATOR, OrganizationInvitation.Role.MEMBER}
MANAGER_ROLES = {OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR}
PROTECTED_FROM_MODERATOR = {OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR}


class OrganizationConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "organization_conflict"
    default_detail = "This action conflicts with the organization's current state."


class OrganizationSuspended(PermissionDenied):
    """403 for anything done inside an organization the administrator has suspended (a ``code`` the frontend can branch on)."""

    default_code = "organization_suspended"
    message = "This organization is suspended."

    def __init__(self):
        super().__init__(detail={"detail": self.message, "code": self.default_code})


class OrganizationPendingOwner(PermissionDenied):
    """403 for anything done in an organization whose owner has not accepted the administrator's invitation yet."""

    default_code = "organization_pending_owner"
    message = "This organization is waiting for its owner to accept the invitation."

    def __init__(self):
        super().__init__(detail={"detail": self.message, "code": self.default_code})


class PersonalWorkspaceIsPrivate(PermissionDenied):
    """403 for anything that would let other people into somebody's personal workspace: it is private, never a shared organization."""

    default_code = "personal_workspace_is_private"
    message = "A personal workspace is private: other people cannot be invited into it or managed in it."

    def __init__(self):
        super().__init__(detail={"detail": self.message, "code": self.default_code})


def assert_shared_organization(organization):
    if organization.is_personal:
        raise PersonalWorkspaceIsPrivate()


def assert_organization_active(organization):
    if organization.status == Organization.Status.SUSPENDED:
        raise OrganizationSuspended()
    if organization.status == Organization.Status.PENDING_OWNER:
        raise OrganizationPendingOwner()


class PlanDowngradeOverCapacity(ValidationError):
    """400 when the administrator lowers a plan below what the organization already uses. Nobody is removed to make room: the people must leave (or the
    invitations be cancelled) first, or the plan stays."""

    def __init__(self, kind, limit, in_use):
        noun = kind[:-1] if limit == 1 else kind
        message = f"This organization already uses {in_use} {kind if in_use != 1 else noun}; the plan allows {limit}. Remove people or cancel invitations first."
        super().__init__(detail={"detail": message, "code": "plan_downgrade_over_capacity", "plan": [message], "kind": kind})
        self.detail["limit"], self.detail["in_use"] = limit, in_use              # DRF turns every value into text; the numbers stay numbers


class PlanLimitReached(PermissionDenied):
    """403 when adding a person would take an organization past what its plan allows (``kind`` and ``limit`` let the frontend say which)."""

    default_code = "plan_limit_reached"

    def __init__(self, kind, limit):
        noun = kind[:-1] if limit == 1 else kind
        super().__init__(detail={"detail": f"Your plan allows {limit} {noun}.", "code": self.default_code, "kind": kind})
        self.detail["limit"] = limit                  # DRF turns every value of a detail dict into text; the limit stays a number


SEAT_KINDS = {OrganizationMembership.Role.MODERATOR: "moderators", OrganizationMembership.Role.MEMBER: "members"}       # the owner takes no seat


def plan_limits(organization):
    """``{"users": n, "moderators": n, "members": n}`` for the organization's plan (``users`` is the total of every role); ``None`` means unlimited (see ``settings.ORGANIZATION_PLAN_LIMITS``)."""
    limits = settings.ORGANIZATION_PLAN_LIMITS.get(organization.plan, {})
    return {"users": limits.get("users"), "moderators": limits.get("moderators"), "members": limits.get("members")}


def seats_in_use(organization, kind, *, with_invitations=True, excluding_user=None):
    """The seats of ``kind`` taken (``users`` = every role together): active memberships, plus (``with_invitations``) the invitations still waiting, which hold a
    seat so that an invitation is never sent that could not be accepted. A person is counted once: an accepted invitation is no longer waiting, it is the
    membership. ``excluding_user`` leaves that person's own membership out."""
    from accounts.models import AccountInvitation

    total = kind == "users"
    role = None if total else next(role for role, name in SEAT_KINDS.items() if name == kind)
    active = OrganizationMembership.objects.filter(organization=organization, status=OrganizationMembership.Status.ACTIVE)
    if not total:
        active = active.filter(role=role)
    if excluding_user is not None:
        active = active.exclude(user=excluding_user)
    count = active.count()
    if with_invitations:
        now = timezone.now()
        waiting = OrganizationInvitation.objects.filter(organization=organization, status=OrganizationInvitation.Status.PENDING, expires_at__gt=now)
        count += (waiting if total else waiting.filter(role=role)).count()
        if total or role == OrganizationMembership.Role.MODERATOR:       # an administrator's invitation to create a moderator account for this organization
            count += AccountInvitation.objects.filter(
                organization=organization, role="moderator", status=AccountInvitation.Status.PENDING, expires_at__gt=now).count()
    return count


def assert_seat_available(organization, role, *, with_invitations=True, excluding_user=None):
    """Raise ``PlanLimitReached`` when one more person with ``role`` would pass the plan's limit. Locks the organization row (inside a transaction) so two
    simultaneous additions cannot both take the last seat."""
    kind = SEAT_KINDS.get(role)
    if kind is None:                       # the owner's own seat is never refused: an organization always has room for the person who owns it
        return
    if transaction.get_connection().in_atomic_block:
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
    limits = plan_limits(organization)
    for counted in ("users", kind):         # the total first: it is the plan's headline number
        limit = limits[counted]
        if limit is not None and seats_in_use(organization, counted, with_invitations=with_invitations, excluding_user=excluding_user) >= limit:
            raise PlanLimitReached(counted, limit)


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
def create_organization(*, name, owner, plan=Organization.Plan.FREE, is_personal=False, actor=None, request=None, moderator_data_scope=None):
    extra = {"moderator_data_scope": moderator_data_scope} if moderator_data_scope else {}      # otherwise the model default: own_members
    organization = Organization.objects.create(
        name=name, slug=_unique_slug(name), plan=plan, is_personal=is_personal, **extra,
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


def organizations_with_head_counts():
    """Every organization with ``member_count`` (active memberships), ``moderator_count``, ``owner_user_id`` and ``owner_username`` (the oldest active owner),
    in one query: what the administrator's list and an organization's own detail show."""
    owner = OrganizationMembership.objects.filter(
        organization=OuterRef("pk"), role=OrganizationMembership.Role.OWNER, status=OrganizationMembership.Status.ACTIVE,
    ).order_by("created_at", "pk")
    active = Q(memberships__status=OrganizationMembership.Status.ACTIVE)
    from accounts.models import AccountInvitation

    now = timezone.now()

    def count_of(queryset):
        return Coalesce(
            Subquery(queryset.filter(organization=OuterRef("pk")).order_by().values("organization").annotate(n=Count("pk")).values("n")[:1], output_field=IntegerField()),
            0,
        )

    Role = OrganizationMembership.Role
    waiting = OrganizationInvitation.objects.filter(status=OrganizationInvitation.Status.PENDING, expires_at__gt=now)
    return Organization.objects.annotate(
        user_seats=count_of(OrganizationMembership.objects.filter(status=OrganizationMembership.Status.ACTIVE))
        + count_of(waiting)
        + count_of(AccountInvitation.objects.filter(role="moderator", status=AccountInvitation.Status.PENDING, expires_at__gt=now)),
        member_count=Count("memberships", filter=active),
        moderator_count=Count("memberships", filter=active & Q(memberships__role=Role.MODERATOR)),
        owner_user_id=Subquery(owner.values("user_id")[:1]),
        owner_username=Subquery(owner.values("user__username")[:1]),
        # Seats in use (see seats_in_use): active people plus invitations still waiting.
        member_seats=count_of(OrganizationMembership.objects.filter(role=Role.MEMBER, status=OrganizationMembership.Status.ACTIVE))
        + count_of(waiting.filter(role=Role.MEMBER)),
        moderator_seats=count_of(OrganizationMembership.objects.filter(role=Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE))
        + count_of(waiting.filter(role=Role.MODERATOR))
        + count_of(AccountInvitation.objects.filter(role="moderator", status=AccountInvitation.Status.PENDING, expires_at__gt=now)),
    )


@transaction.atomic
def update_organization(
    *, organization, actor, name=None, plan=None, status=None, moderator_data_scope=None, brand_name=None, brand_accent=None, reason="", request=None,
    download_approval_required=None, download_grant_hours=None, clear_download_grant_hours=False,
):
    """Rename an organization, change its plan or what its moderators see, suspend or reactivate it: one transaction, one audit event per kind of change.

    The caller decides who may change what (the administrator: all of it; the owner: ``name`` and ``moderator_data_scope`` only). Only what
    actually changes is written and audited. Suspending keeps every record as it is (members, photographs, reviews); reactivating therefore
    restores everything. A personal workspace cannot be suspended: that would lock a person out of their own photographs, and the way to stop a
    person is to suspend the account.
    """
    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    if status == Organization.Status.PENDING_OWNER:
        raise ValidationError({"status": "An organization waits for its owner only when it is created with an owner e-mail address."})
    if status == Organization.Status.ACTIVE and locked.status == Organization.Status.PENDING_OWNER:
        raise ValidationError({"status": "This organization becomes active when its owner accepts the invitation; nobody can activate it without an owner."})
    if status == Organization.Status.ACTIVE and locked.status == Organization.Status.SUSPENDED and not locked.is_personal and not OrganizationMembership.objects.filter(
        organization=locked, role=OrganizationMembership.Role.OWNER, status=OrganizationMembership.Status.ACTIVE,
    ).exists():
        status = Organization.Status.PENDING_OWNER          # suspended before its owner ever accepted: it goes back to waiting for them, never to "active" without an owner
    if status == Organization.Status.SUSPENDED and locked.is_personal:
        raise ValidationError({"status": "A personal workspace cannot be suspended. Suspend the account instead."})
    if plan is not None and plan != locked.plan:
        new_limits = settings.ORGANIZATION_PLAN_LIMITS.get(plan, {})
        for kind in ("users", "moderators", "members"):
            limit = new_limits.get(kind)
            in_use = seats_in_use(locked, kind)
            if limit is not None and in_use > limit:
                raise PlanDowngradeOverCapacity(kind, limit, in_use)
    Action = OrganizationAuditEvent.Action
    details = {}
    if name is not None and name != locked.name:
        details["name"] = (locked.name, name)
    if plan is not None and plan != locked.plan:
        details["plan"] = (locked.plan, plan)
    if moderator_data_scope is not None and moderator_data_scope != locked.moderator_data_scope:
        details["moderator_data_scope"] = (locked.moderator_data_scope, moderator_data_scope)
    if brand_name is not None and brand_name != locked.brand_name:              # "" clears; None means "not sent"
        details["brand_name"] = (locked.brand_name, brand_name)
    if brand_accent is not None and brand_accent != locked.brand_accent:
        details["brand_accent"] = (locked.brand_accent, brand_accent)
    if download_approval_required:               # asking for it is checked every time, also when the value would not change
        if locked.is_personal:
            raise ValidationError({"download_approval_required": "A personal workspace has no moderator to approve downloads."})
        if not OrganizationMembership.objects.filter(
            organization=locked, role=OrganizationMembership.Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE, user__is_active=True, user__is_suspended=False,
        ).exists():
            # Approval needs somebody to give it, and nobody approves their own request: the owner's own requests need a moderator.
            raise ValidationError({"download_approval_required": "Add at least one active moderator to this organization before turning on download approval."})
    if download_approval_required is not None and download_approval_required != locked.download_approval_required:
        details["download_approval_required"] = (locked.download_approval_required, download_approval_required)
    if clear_download_grant_hours and locked.download_grant_hours is not None:
        details["download_grant_hours"] = (locked.download_grant_hours, None)
    elif download_grant_hours is not None and download_grant_hours != locked.download_grant_hours:
        details["download_grant_hours"] = (locked.download_grant_hours, download_grant_hours)
    changed_status = status is not None and status != locked.status
    previous_status = locked.status
    for field, (_, new) in details.items():
        setattr(locked, field, new)
    if changed_status:
        locked.status = status
    if details or changed_status:
        locked.save(update_fields=[*details, *(["status"] if changed_status else []), "updated_at"])
    if details:
        record_audit_event(
            locked, actor, Action.ORGANIZATION_UPDATED, target_type="Organization", target_id=locked.pk,
            previous_value={field: old for field, (old, _) in details.items()}, new_value={field: new for field, (_, new) in details.items()},
            reason=reason, request=request,
        )
    if changed_status:
        record_audit_event(
            locked, actor, Action.ORGANIZATION_SUSPENDED if status == Organization.Status.SUSPENDED else Action.ORGANIZATION_REACTIVATED,
            target_type="Organization", target_id=locked.pk,
            previous_value={"status": previous_status}, new_value={"status": status}, reason=reason, request=request,
        )
    return locked


def personal_organization_of(user):
    """The user's personal workspace (the organization every account owns alone, ``is_personal``), or ``None`` when the account has none."""
    return (
        Organization.objects.filter(
            is_personal=True, memberships__user=user, memberships__role=OrganizationMembership.Role.OWNER, memberships__status=OrganizationMembership.Status.ACTIVE,
        ).order_by("pk").first()
    )


def ensure_personal_organization(user):
    """The user's personal workspace, created if the account has none (an account made by an administrator, or from before workspaces). Safe to call twice at once."""
    existing = personal_organization_of(user)
    if existing is not None:
        return existing
    with transaction.atomic():
        type(user).objects.select_for_update().get(pk=user.pk)
        return personal_organization_of(user) or create_personal_organization(user)


def get_default_organization(user):
    """The workspace new content is filed under when the person did not choose one: **their own personal workspace and nothing else**.

    An organization is never inferred from the person's memberships: somebody who works for an organization and has no personal workspace (an account an administrator
    made before workspaces were ensured) gets ``None``, "no organization", exactly like a private photograph, instead of having their own photographs filed under the
    organization where its staff would see them. Filing something under an organization takes the person's explicit choice (``resolve_upload_organization``).
    """
    organization = personal_organization_of(user)
    return organization if organization is not None and organization.status == Organization.Status.ACTIVE else None


def resolve_upload_organization(user, organization_id=None):
    """The organization an upload is filed under.

    Without ``organization_id`` the person's own personal workspace (see ``get_default_organization``): never an organization they merely belong to. With one, the person must be an active member of it: submitting photographs to an organization is how its moderators come to see them, and it
    is the uploader's explicit choice. Raises a field error on ``organization_id`` otherwise.
    """
    from rest_framework.exceptions import ValidationError

    if organization_id in (None, ""):
        return get_default_organization(user)
    try:
        organization_id = int(organization_id)
    except (TypeError, ValueError):
        raise ValidationError({"organization_id": ["Send the number of one of your organizations."]}) from None
    membership = get_active_membership(user, organization_id)
    if membership is None:
        raise ValidationError({"organization_id": ["You are not an active member of that organization."]})
    assert_organization_active(membership.organization)
    return membership.organization


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
    """Send the moderator/member invitation and record the outcome on the invitation (``email_status``). A mail server that refuses or cannot be reached no longer turns the
    request into a 500 with an invitation left behind: the invitation exists, says ``failed`` (with a short reason that never contains the link), and can be resent."""
    link = f"{settings.FRONTEND_URL.rstrip('/')}/invitations/{token}"
    try:
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
    except Exception as error:  # noqa: BLE001 - recorded, never raised
        OrganizationInvitation.objects.filter(pk=invitation.pk).update(
            email_status=OrganizationInvitation.EmailStatus.FAILED, email_failure_reason=_short_reason(error, token), updated_at=timezone.now(),
        )
        invitation.email_status, invitation.email_failure_reason = OrganizationInvitation.EmailStatus.FAILED, _short_reason(error, token)
        logger.warning("The invitation e-mail %s could not be sent (%s).", invitation.pk, type(error).__name__)          # no address, no link, no token
        return False
    OrganizationInvitation.objects.filter(pk=invitation.pk).update(
        email_status=OrganizationInvitation.EmailStatus.SENT, email_sent_at=timezone.now(), email_failure_reason="", updated_at=timezone.now(),
    )
    invitation.email_status, invitation.email_failure_reason = OrganizationInvitation.EmailStatus.SENT, ""
    return True


def invite_member(*, organization, actor_membership, email, role, request=None):
    assert_shared_organization(organization)
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
    with transaction.atomic():
        assert_seat_available(organization, role)                      # the invitation holds a seat until it is accepted, cancelled or expires
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


def _refuse_owner_invitation(invitation):
    if invitation.role == OrganizationInvitation.Role.OWNER:
        raise PermissionDenied("An owner invitation is managed by the FaceCloud administrator.")


def resend_invitation(*, invitation, actor_membership, request=None):
    assert_shared_organization(invitation.organization)
    _refuse_owner_invitation(invitation)
    if invitation.status != OrganizationInvitation.Status.PENDING:
        raise OrganizationConflict("Only a pending invitation can be resent.")
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        if invitation.is_expired():                                    # an expired invitation holds no seat: bringing it back needs one
            assert_seat_available(invitation.organization, invitation.role)
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
    _refuse_owner_invitation(invitation)
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
def preview_invitation(token):
    """The pending, unexpired invitation a token belongs to, or ``None`` for an unknown, used, cancelled or expired one.

    Nothing distinguishes those cases to the caller: the answer to "is this token good" is the only thing a holder of a token may learn.
    """
    invitation = (
        OrganizationInvitation.objects.select_related("organization")
        .filter(token_hash=hash_token(token or ""), status=OrganizationInvitation.Status.PENDING)
        .first()
    )
    if invitation is None or invitation.is_expired():
        return None
    allowed = {Organization.Status.ACTIVE}
    if invitation.role == OrganizationInvitation.Role.OWNER:
        allowed.add(Organization.Status.PENDING_OWNER)       # the owner invitation is what makes the organization active
    if invitation.organization.status not in allowed:
        return None                                          # a suspended organization cannot take anyone in: the token is no good for now
    return invitation


def accept_invitation(*, token, user, request=None):
    """Accept an invitation as ``user`` (who must hold the invited e-mail address). One transaction: the row lock needs it, and an owner's acceptance also activates the organization."""
    with transaction.atomic():
        invitation = (
            OrganizationInvitation.objects.select_for_update(of=("self",))
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
        if invitation.role == OrganizationInvitation.Role.OWNER:
            return _accept_owner_invitation(invitation, user, request)
        assert_shared_organization(invitation.organization)                      # nobody joins a personal workspace, whatever token they hold
        assert_organization_active(invitation.organization)
        assert_seat_available(invitation.organization, invitation.role, with_invitations=False, excluding_user=user)       # re-checked: the plan or the team may have changed
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


def _accept_owner_invitation(invitation, user, request):
    """The invited person becomes the owner and the organization becomes active, in one step (the caller holds the invitation's row lock and a transaction)."""
    organization = Organization.objects.select_for_update().get(pk=invitation.organization_id)
    if organization.status == Organization.Status.SUSPENDED:
        raise OrganizationSuspended()
    if organization.status != Organization.Status.PENDING_OWNER:
        raise OrganizationConflict("This invitation is no longer valid.")
    # Knowing the link is not enough: the account must be active and its e-mail address verified, and it must be the address the invitation was sent to (checked by the caller).
    if not user.is_active or getattr(user, "is_suspended", False):
        raise PermissionDenied("This account cannot take ownership of an organization.")
    if not user.is_email_verified:
        raise PermissionDenied("Verify your email address before accepting this invitation.")
    if OrganizationMembership.objects.filter(organization=organization, role=OrganizationMembership.Role.OWNER, status=OrganizationMembership.Status.ACTIVE).exists():
        raise OrganizationConflict("This organization already has an owner.")
    membership, _ = OrganizationMembership.objects.update_or_create(
        organization=organization, user=user,
        defaults={"role": OrganizationMembership.Role.OWNER, "status": OrganizationMembership.Status.ACTIVE, "invited_by": invitation.invited_by},
    )
    organization.status = Organization.Status.ACTIVE
    organization.save(update_fields=["status", "updated_at"])
    invitation.status = OrganizationInvitation.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["status", "accepted_at", "updated_at"])
    record_audit_event(
        organization, user, OrganizationAuditEvent.Action.INVITATION_ACCEPTED,
        target_type="OrganizationMembership", target_id=membership.pk, new_value={"role": membership.role}, request=request,
    )
    record_audit_event(
        organization, user, OrganizationAuditEvent.Action.ORGANIZATION_ACTIVATED,
        target_type="Organization", target_id=organization.pk, previous_value={"status": Organization.Status.PENDING_OWNER},
        new_value={"status": Organization.Status.ACTIVE, "owner": user.pk}, request=request,
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
    assert_shared_organization(membership.organization)
    locked = OrganizationMembership.objects.select_for_update().get(pk=membership.pk)
    _assert_can_manage(actor_membership, locked)
    if role == OrganizationMembership.Role.OWNER and actor_membership.role != OrganizationMembership.Role.OWNER:
        raise PermissionDenied("Only an organization owner can grant the owner role.")
    # The same rule as an invitation (invite_member): a moderator hands out only the plain member role.
    if (
        role is not None
        and role != OrganizationMembership.Role.MEMBER
        and role != locked.role
        and actor_membership.role != OrganizationMembership.Role.OWNER
    ):
        raise PermissionDenied("Moderators may only set the normal member role.")
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
    if locked.status == OrganizationMembership.Status.ACTIVE and (previous["status"] != locked.status or previous["role"] != locked.role):
        # Promoting, demoting or reactivating takes a seat of the new kind. After the last-owner rules above, so the more basic refusal comes first.
        assert_seat_available(locked.organization, locked.role, excluding_user=locked.user)
    locked.save(update_fields=["role", "status", "updated_at"])
    released = 0
    if not (
        locked.status == OrganizationMembership.Status.ACTIVE and locked.role in MANAGER_ROLES
    ):
        from moderation.services import release_open_reviews

        released = release_open_reviews(organization=locked.organization, user=locked.user)
    action = (
        OrganizationAuditEvent.Action.MEMBER_ROLE_CHANGED if role is not None
        else OrganizationAuditEvent.Action.MEMBER_STATUS_CHANGED
    )
    if (
        locked.role == OrganizationMembership.Role.MODERATOR and previous["role"] != locked.role
        and locked.status == OrganizationMembership.Status.ACTIVE
    ):
        from accounts.moderator_emails import send_made_moderator

        send_made_moderator(locked.user, locked.organization)
    record_audit_event(
        locked.organization, actor_membership.user, action,
        target_type="OrganizationMembership", target_id=locked.pk,
        previous_value=previous,
        new_value={"role": locked.role, "status": locked.status, **({"released_reviews": released} if released else {})},
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
    from moderation.services import release_open_reviews

    released = release_open_reviews(organization=locked.organization, user=locked.user)
    record_audit_event(
        locked.organization, actor_membership.user, OrganizationAuditEvent.Action.MEMBER_REMOVED,
        target_type="OrganizationMembership", target_id=locked.pk,
        previous_value=previous, new_value={"status": locked.status, "released_reviews": released},
        reason=reason, request=request,
    )
    return locked


# ---- The owner of a new organization (Platform Administrator -> owner invitation) -----------------------------------------------------------------------------------------------
#
# The administrator names the organization, the owner's e-mail address and the plan. Nothing requires the owner to have an account: the organization is created waiting for its
# owner (``Organization.Status.PENDING_OWNER``, nobody belongs to it, nothing can be done in it) together with an ordinary ``OrganizationInvitation`` with the role ``owner``: the same
# hashed single-use token, expiry, preview and acceptance as every other organization invitation. Accepting it (signing in as the invited address, or registering with it through
# ``POST /api/auth/register/`` with ``invitation_token``) makes that account the owner and the organization active in one transaction.


def owner_invitation_of(organization):
    """The latest owner invitation of ``organization``, or ``None``."""
    return organization.invitations.filter(role=OrganizationInvitation.Role.OWNER).order_by("-created_at", "-pk").first()


def effective_invitation_status(invitation):
    """``expired`` for a pending invitation whose time has run out (the stored status changes only when somebody acts on it)."""
    if invitation.status == OrganizationInvitation.Status.PENDING and invitation.is_expired():
        return OrganizationInvitation.Status.EXPIRED
    return invitation.status


def _owner_invitation_expiry():
    return timezone.now() + timedelta(hours=settings.ORG_OWNER_INVITATION_TTL_HOURS)


def _short_reason(error, token):
    """What a person may read about a failed delivery: the kind of error and the start of its message, with the token (if the message held it) removed."""
    text = f"{type(error).__name__}: {error}".replace(token, "[removed]") if token else f"{type(error).__name__}: {error}"
    text = re.sub(r"https?://\S+", "[link removed]", text)           # a mail library may quote the whole message: no link, whole or partial, is ever stored or shown
    return text[:190]


def _deliver_owner_invitation_email(invitation_id, token):
    """Send the owner invitation e-mail (called after the transaction that made or renewed the invitation has committed) and record the outcome. Never raises.

    The e-mail goes out in this process, not through the task queue: the link holds the raw token, which is stored nowhere (only its hash is), and a queue would hold it. A failure
    leaves the organization and the invitation as they are with ``email_status: failed``; the administrator resends (a new link) from the same screen.
    """
    expected_hash = hash_token(token)
    invitation = OrganizationInvitation.objects.select_related("organization").filter(pk=invitation_id, token_hash=expected_hash).first()
    if invitation is None or invitation.status != OrganizationInvitation.Status.PENDING:
        return                                                  # renewed or cancelled since: this link is no longer the one to send
    link = f"{settings.FRONTEND_URL.rstrip('/')}/invitations/{token}"
    hours = max(1, round((invitation.expires_at - timezone.now()).total_seconds() / 3600))
    organization = invitation.organization
    try:
        send_mail(
            subject=f"You have been invited to own {organization.name} on FaceCloud",
            message=(
                f"FaceCloud has set up the organization \"{organization.name}\" and invited {invitation.email} to be its owner.\n\n"
                f"Accept the invitation: {link}\n\n"
                "Sign in with this e-mail address, or create an account with it, and the invitation makes you the owner. "
                f"The link works once and expires on {invitation.expires_at:%Y-%m-%d %H:%M} UTC (in about {hours} hours).\n\n"
                "If you were not expecting this, ignore this e-mail: nothing happens until the link is used. FaceCloud never sends a password by e-mail."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[invitation.email], fail_silently=False,
        )
    except Exception as error:
        OrganizationInvitation.objects.filter(pk=invitation_id, token_hash=expected_hash).update(
            email_status=OrganizationInvitation.EmailStatus.FAILED, email_failure_reason=_short_reason(error, token), updated_at=timezone.now(),
        )
        logger.warning("The owner invitation e-mail %s could not be sent (%s).", invitation_id, type(error).__name__)       # no address, no link, no token
        return
    OrganizationInvitation.objects.filter(pk=invitation_id, token_hash=expected_hash).update(
        email_status=OrganizationInvitation.EmailStatus.SENT, email_sent_at=timezone.now(), email_failure_reason="", updated_at=timezone.now(),
    )


def _queue_owner_invitation_email(invitation, token):
    transaction.on_commit(lambda: _deliver_owner_invitation_email(invitation.pk, token))


def _lock_for(key):
    """Serialize the requests that carry the same ``key`` (PostgreSQL advisory lock held to the end of the transaction); on SQLite writes are serialized anyway."""
    connection = transaction.get_connection()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])


def create_pending_organization(*, name, owner_email, plan=Organization.Plan.FREE, actor, request=None, moderator_data_scope=None):
    """Create an organization for an owner who has not accepted yet, and queue the invitation e-mail. Returns ``(organization, invitation, created)``.

    The person may or may not have an account: either way nothing is granted until they accept as the invited address. Sending the same request again (same name and owner e-mail while
    that organization is still waiting) returns the organization and invitation that exist, with ``created`` false, and sends nothing.
    """
    email = owner_email.strip().lower()
    name = " ".join(name.split())
    token = secrets.token_urlsafe(32)
    extra = {"moderator_data_scope": moderator_data_scope} if moderator_data_scope else {}
    with transaction.atomic():
        _lock_for(f"pending-organization:{name.casefold()}:{email}")
        existing = (
            OrganizationInvitation.objects.select_related("organization")
            .filter(role=OrganizationInvitation.Role.OWNER, email=email, organization__status=Organization.Status.PENDING_OWNER,
                    organization__is_personal=False, organization__name__iexact=name)
            .order_by("-created_at", "-pk").first()
        )
        if existing is not None:
            return existing.organization, existing, False
        organization = Organization.objects.create(name=name, slug=_unique_slug(name), plan=plan, status=Organization.Status.PENDING_OWNER, **extra)
        invitation = OrganizationInvitation.objects.create(
            organization=organization, email=email, role=OrganizationInvitation.Role.OWNER, token_hash=hash_token(token),
            status=OrganizationInvitation.Status.PENDING, invited_by=actor, expires_at=_owner_invitation_expiry(),
            email_status=OrganizationInvitation.EmailStatus.QUEUED,
        )
        record_audit_event(
            organization, actor, OrganizationAuditEvent.Action.ORGANIZATION_CREATED, target_type="Organization", target_id=organization.pk,
            new_value={"name": name, "plan": plan, "status": Organization.Status.PENDING_OWNER, "pending_owner_email": email}, request=request,
        )
        record_audit_event(
            organization, actor, OrganizationAuditEvent.Action.OWNER_INVITED, target_type="OrganizationInvitation", target_id=invitation.pk,
            new_value={"email": email, "expires_at": invitation.expires_at.isoformat()}, request=request,
        )
        _queue_owner_invitation_email(invitation, token)
    return organization, invitation, True


def resend_owner_invitation(*, organization, actor, request=None):
    """Send the owner a new link (the old one stops working). Allowed while the organization is waiting for its owner, for a pending, expired or cancelled invitation."""
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        locked = Organization.objects.select_for_update().get(pk=organization.pk)
        if locked.status == Organization.Status.SUSPENDED:
            raise OrganizationSuspended()
        invitation = (
            OrganizationInvitation.objects.select_for_update(of=("self",))
            .filter(organization=locked, role=OrganizationInvitation.Role.OWNER).order_by("-created_at", "-pk").first()
        )
        if invitation is None:
            raise NotFound("This organization has no owner invitation.")
        if locked.status != Organization.Status.PENDING_OWNER or invitation.status == OrganizationInvitation.Status.ACCEPTED:
            raise OrganizationConflict("The owner has already accepted this invitation.")
        invitation.token_hash = hash_token(token)
        invitation.status = OrganizationInvitation.Status.PENDING
        invitation.expires_at = _owner_invitation_expiry()
        invitation.email_status = OrganizationInvitation.EmailStatus.QUEUED
        invitation.email_sent_at = None
        invitation.email_failure_reason = ""
        invitation.save(update_fields=["token_hash", "status", "expires_at", "email_status", "email_sent_at", "email_failure_reason", "updated_at"])
        record_audit_event(
            locked, actor, OrganizationAuditEvent.Action.INVITATION_RESENT, target_type="OrganizationInvitation", target_id=invitation.pk,
            new_value={"role": "owner", "expires_at": invitation.expires_at.isoformat()}, request=request,
        )
        _queue_owner_invitation_email(invitation, token)
    return invitation


def cancel_owner_invitation(*, organization, actor, request=None):
    """Withdraw the pending owner invitation. The organization stays as it is (waiting for an owner); the administrator may send a new invitation with a resend."""
    with transaction.atomic():
        invitation = (
            OrganizationInvitation.objects.select_for_update(of=("self",))
            .filter(organization=organization, role=OrganizationInvitation.Role.OWNER).order_by("-created_at", "-pk").first()
        )
        if invitation is None:
            raise NotFound("This organization has no owner invitation.")
        if invitation.status != OrganizationInvitation.Status.PENDING:
            raise OrganizationConflict("Only a pending invitation can be cancelled.")
        invitation.status = OrganizationInvitation.Status.CANCELLED
        invitation.save(update_fields=["status", "updated_at"])
        record_audit_event(
            organization, actor, OrganizationAuditEvent.Action.INVITATION_CANCELLED, target_type="OrganizationInvitation", target_id=invitation.pk,
            new_value={"role": "owner"}, request=request,
        )
    return invitation

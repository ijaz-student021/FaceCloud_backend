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
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
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


class OrganizationSuspended(PermissionDenied):
    """403 for anything done inside an organization the administrator has suspended (a ``code`` the frontend can branch on)."""

    default_code = "organization_suspended"
    message = "This organization is suspended."

    def __init__(self):
        super().__init__(detail={"detail": self.message, "code": self.default_code})


def assert_organization_active(organization):
    if organization.status == Organization.Status.SUSPENDED:
        raise OrganizationSuspended()


class PlanLimitReached(PermissionDenied):
    """403 when adding a person would take an organization past what its plan allows (``kind`` and ``limit`` let the frontend say which)."""

    default_code = "plan_limit_reached"

    def __init__(self, kind, limit):
        noun = kind[:-1] if limit == 1 else kind
        super().__init__(detail={"detail": f"Your plan allows {limit} {noun}.", "code": self.default_code, "kind": kind})
        self.detail["limit"] = limit                  # DRF turns every value of a detail dict into text; the limit stays a number


SEAT_KINDS = {OrganizationMembership.Role.MODERATOR: "moderators", OrganizationMembership.Role.MEMBER: "members"}       # the owner takes no seat


def plan_limits(organization):
    """``{"moderators": n, "members": n}`` for the organization's plan; ``None`` means unlimited (see ``settings.ORGANIZATION_PLAN_LIMITS``)."""
    limits = settings.ORGANIZATION_PLAN_LIMITS.get(organization.plan, {})
    return {"moderators": limits.get("moderators"), "members": limits.get("members")}


def seats_in_use(organization, kind, *, with_invitations=True, excluding_user=None):
    """The seats of ``kind`` taken: active memberships of that role, plus (``with_invitations``) the invitations still waiting, which hold a seat so that an
    invitation is never sent that could not be accepted. ``excluding_user`` leaves that person's own membership out."""
    from accounts.models import AccountInvitation

    role = next(role for role, name in SEAT_KINDS.items() if name == kind)
    active = OrganizationMembership.objects.filter(organization=organization, role=role, status=OrganizationMembership.Status.ACTIVE)
    if excluding_user is not None:
        active = active.exclude(user=excluding_user)
    count = active.count()
    if with_invitations:
        now = timezone.now()
        count += OrganizationInvitation.objects.filter(
            organization=organization, role=role, status=OrganizationInvitation.Status.PENDING, expires_at__gt=now).count()
        if role == OrganizationMembership.Role.MODERATOR:       # an administrator's invitation to create a moderator account for this organization
            count += AccountInvitation.objects.filter(
                organization=organization, role="moderator", status=AccountInvitation.Status.PENDING, expires_at__gt=now).count()
    return count


def assert_seat_available(organization, role, *, with_invitations=True, excluding_user=None):
    """Raise ``PlanLimitReached`` when one more person with ``role`` would pass the plan's limit. Locks the organization row (inside a transaction) so two
    simultaneous additions cannot both take the last seat."""
    kind = SEAT_KINDS.get(role)
    if kind is None:
        return
    if transaction.get_connection().in_atomic_block:
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
    limit = plan_limits(organization)[kind]
    if limit is not None and seats_in_use(organization, kind, with_invitations=with_invitations, excluding_user=excluding_user) >= limit:
        raise PlanLimitReached(kind, limit)


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
def update_organization(*, organization, actor, name=None, plan=None, status=None, moderator_data_scope=None, reason="", request=None):
    """Rename an organization, change its plan or what its moderators see, suspend or reactivate it: one transaction, one audit event per kind of change.

    The caller decides who may change what (the administrator: all of it; the owner: ``name`` and ``moderator_data_scope`` only). Only what
    actually changes is written and audited. Suspending keeps every record as it is (members, photographs, reviews); reactivating therefore
    restores everything. A personal workspace cannot be suspended: that would lock a person out of their own photographs, and the way to stop a
    person is to suspend the account.
    """
    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    if status == Organization.Status.SUSPENDED and locked.is_personal:
        raise ValidationError({"status": "A personal workspace cannot be suspended. Suspend the account instead."})
    Action = OrganizationAuditEvent.Action
    details = {}
    if name is not None and name != locked.name:
        details["name"] = (locked.name, name)
    if plan is not None and plan != locked.plan:
        details["plan"] = (locked.plan, plan)
    if moderator_data_scope is not None and moderator_data_scope != locked.moderator_data_scope:
        details["moderator_data_scope"] = (locked.moderator_data_scope, moderator_data_scope)
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


def get_default_organization(user):
    """The organization new content the user creates should be tagged with.

    Prefers their personal organization (every user has exactly one, from
    ``create_personal_organization``) so ownership keeps behaving the way it
    always has; falls back to their oldest other active membership, and to
    ``None`` only for a user with no organization at all (a test fixture
    created directly rather than through registration). A suspended organization is never the default.
    """
    membership = (
        OrganizationMembership.objects.select_related("organization")
        .filter(user=user, status=OrganizationMembership.Status.ACTIVE, organization__status=Organization.Status.ACTIVE)
        .order_by("-organization__is_personal", "created_at")
        .first()
    )
    return membership.organization if membership else None


def resolve_upload_organization(user, organization_id=None):
    """The organization an upload is filed under.

    Without ``organization_id`` the default (the person's own workspace, see ``get_default_organization``), so nothing changes for anyone who does not
    choose. With one, the person must be an active member of it: submitting photographs to an organization is how its moderators come to see them, and it
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


def resend_invitation(*, invitation, actor_membership, request=None):
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
    if invitation is None or invitation.is_expired() or invitation.organization.status != Organization.Status.ACTIVE:
        return None                                          # a suspended organization cannot take anyone in: the token is no good for now
    return invitation


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

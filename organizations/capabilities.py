"""What the current member may do in an organization, for clients to show.

Derived from the same role constants the services and views enforce
(``services.MANAGER_ROLES``, ``services.PROTECTED_FROM_MODERATOR``,
``OrganizationScopedMixin.require_owner_or_moderator``), so the list cannot
promise an action the server refuses. It is advisory: every endpoint still
checks membership itself, and a client must handle 403/404 regardless.
"""
from .models import OrganizationMembership
from .services import MANAGER_ROLES, PROTECTED_FROM_MODERATOR

Role = OrganizationMembership.Role

CAPABILITY_NAMES = (
    "view_members",          # GET members/, invitations/
    "invite_members",        # invite with role "member"
    "invite_moderators",     # invite with role "moderator"
    "manage_members",        # update/remove a plain member
    "manage_moderators",     # update/remove a moderator or owner
    "grant_owner",           # set role "owner"
    "view_audit",            # GET audit/
    "moderate_reviews",      # moderator/reviews/* (claim, decide, escalate, bulk)
    "manage_batches",        # moderator/batches/* (list, retry, cancel)
    "create_reports",        # moderator/reports/*
    "export_organization",   # export/*
)


def membership_capabilities(membership):
    """``{name: bool}`` for an active membership; all False for anything else."""
    # Nothing is possible inside a suspended organization (OrganizationScopedMixin answers 403), so none of it is offered.
    active = (
        membership is not None and membership.status == OrganizationMembership.Status.ACTIVE
        and membership.organization.status == membership.organization.Status.ACTIVE
    )
    role = membership.role if active else None
    staff = role in MANAGER_ROLES                      # require_owner_or_moderator views
    owner = role == Role.OWNER
    # The organization-wide export covers every member's data: with moderators limited to their own members, only the owner may run it.
    scoped_moderator = (
        role == Role.MODERATOR and membership.organization.moderator_data_scope == membership.organization.ModeratorDataScope.OWN_MEMBERS
    )
    # services._assert_can_manage: a moderator cannot manage PROTECTED_FROM_MODERATOR.
    manages_protected = staff and (owner or not PROTECTED_FROM_MODERATOR)
    return {
        "view_members": staff,
        "invite_members": staff,
        "invite_moderators": owner,                    # services.invite_member
        "manage_members": staff,
        "manage_moderators": manages_protected,
        "grant_owner": owner,                          # services.update_member
        "view_audit": staff,
        "moderate_reviews": staff,
        "manage_batches": staff,
        "create_reports": staff,
        "export_organization": staff and not scoped_moderator,
    }


def membership_summary(membership):
    """The caller's own membership as clients need it, or None."""
    if membership is None:
        return None
    return {"id": membership.pk, "role": membership.role, "status": membership.status}

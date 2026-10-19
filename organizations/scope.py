"""What a moderator may see inside their organization (``Organization.moderator_data_scope``).

Every organization-scoped moderator endpoint asks this module instead of deciding for itself, so the rule has one definition.

| Who | Setting ``organization`` | Setting ``own_members`` (the default for new organizations) |
|---|---|---|
| Organization **owner** | everything | everything |
| **Moderator** | everything | the members **they invited**, plus every member whom **no current moderator invited** (invited by the administrator, by an owner, by nobody, or by someone who is no longer a moderator): nothing is left without a reviewer. Never the members another current moderator invited. |

What "a member's data" is: whatever they own that was filed under the organization (batches and photographs, their reviews, the person folders built from them),
the membership itself and invitations. An id outside the visible set answers **404**, exactly like an id from another organization (no 403: it would say the thing exists).

"Current moderator" means an active moderator membership of that organization right now: remove or demote the inviting moderator and their members become visible to
every moderator at once, because the rule is computed from the live memberships on each request.
"""
from functools import cached_property

from django.db.models import Q

from .models import Organization, OrganizationMembership

Role = OrganizationMembership.Role
Status = OrganizationMembership.Status
OWN_MEMBERS = Organization.ModeratorDataScope.OWN_MEMBERS
ORGANIZATION = Organization.ModeratorDataScope.ORGANIZATION


class DataScope:
    """The members whose data ``viewer`` may not see in ``organization`` (empty for an owner, or when the organization lets moderators see everything)."""

    def __init__(self, organization, viewer, membership=None):
        self.organization = organization
        self.viewer = viewer
        self.membership = membership

    @cached_property
    def restricted(self):
        """True only for an active moderator of an organization that uses ``own_members``."""
        return (
            self.membership is not None and self.membership.status == Status.ACTIVE and self.membership.role == Role.MODERATOR
            and self.organization.moderator_data_scope == OWN_MEMBERS
        )

    @cached_property
    def other_moderator_ids(self):
        """The other current moderators of the organization (empty unless the viewer is restricted)."""
        if not self.restricted:
            return frozenset()
        return frozenset(
            OrganizationMembership.objects.filter(organization=self.organization, role=Role.MODERATOR, status=Status.ACTIVE)
            .exclude(user=self.viewer).values_list("user_id", flat=True)
        )

    @cached_property
    def hidden_user_ids(self):
        """Ids of the members (not staff) another current moderator invited."""
        if not self.other_moderator_ids:
            return frozenset()
        return frozenset(
            OrganizationMembership.objects.filter(
                organization=self.organization, role=Role.MEMBER, invited_by_id__in=self.other_moderator_ids,
            ).values_list("user_id", flat=True)
        )

    def exclude_invitations(self, queryset):
        """``queryset`` of organization invitations without those another current moderator sent."""
        return queryset.exclude(invited_by_id__in=self.other_moderator_ids) if self.other_moderator_ids else queryset

    def exclude_memberships(self, queryset):
        """``queryset`` of memberships without the members another current moderator invited."""
        return queryset.exclude(user_id__in=self.hidden_user_ids) if self.hidden_user_ids else queryset

    def exclude(self, queryset, owner_field="owner_id"):
        """``queryset`` without rows owned by a member this viewer may not see (``owner_field`` is the lookup that names the owner)."""
        if not self.hidden_user_ids:
            return queryset
        return queryset.exclude(**{f"{owner_field}__in": self.hidden_user_ids})

    def sees(self, user_id):
        return user_id not in self.hidden_user_ids

    def hidden_filter(self, owner_field="owner_id"):
        """A ``Q`` that matches rows this viewer may NOT see (never matches anything for an unrestricted viewer)."""
        return Q(**{f"{owner_field}__in": self.hidden_user_ids}) if self.hidden_user_ids else Q(pk__in=[])


def data_scope(organization, user):
    """The viewer's scope in ``organization``, looking their active membership up (for code that has no request, such as tasks and notifications)."""
    membership = OrganizationMembership.objects.filter(organization=organization, user=user, status=Status.ACTIVE).first()
    return DataScope(organization, user, membership)


def moderators_who_see(organization, owner_id):
    """The active owners and moderators of ``organization`` who may see ``owner_id``'s data (the people to notify about it)."""
    staff = OrganizationMembership.objects.filter(
        organization=organization, status=Status.ACTIVE, role__in=(Role.OWNER, Role.MODERATOR),
    ).select_related("user")
    return [membership for membership in staff if DataScope(organization, membership.user, membership).sees(owner_id)]


def visible_audit_events(queryset, organization, scope):
    """The audit events ``scope``'s viewer may read: ``queryset`` (the organization's events) minus those about a member they may not see.

    A restricted moderator reads their **own actions**, and every event except (a) events whose target belongs to a hidden member (a review, upload,
    photograph, person folder, membership or invitation) and (b) events another current moderator recorded without a single target, such as a bulk decision, whose
    new value lists that moderator's own reviews. An unrestricted viewer (an owner, or an organization that lets moderators see everything) reads everything.
    """
    if scope is None or not scope.restricted:
        return queryset
    from identities.models import IdentityGroup
    from moderation.models import ModeratorReview
    from uploads.models import ImageUpload, UploadBatch

    hidden = scope.hidden_user_ids
    other_moderators = list(
        OrganizationMembership.objects.filter(organization=organization, role=Role.MODERATOR, status=Status.ACTIVE)
        .exclude(user=scope.viewer).values_list("user_id", flat=True)
    )
    about_hidden = (
        Q(target_type="ModeratorReview", target_id__in=ModeratorReview.objects.filter(organization=organization, image__owner_id__in=hidden).values("pk"))
        | Q(target_type="UploadBatch", target_id__in=UploadBatch.objects.filter(organization=organization, owner_id__in=hidden).values("pk"))
        | Q(target_type="ImageUpload", target_id__in=ImageUpload.objects.filter(batch__organization=organization, owner_id__in=hidden).values("pk"))
        | Q(target_type="IdentityGroup", target_id__in=IdentityGroup.objects.filter(owner_id__in=hidden).values("pk"))
        | Q(target_type="OrganizationMembership", target_id__in=OrganizationMembership.objects.filter(organization=organization, user_id__in=hidden).values("pk"))
        | Q(target_type="OrganizationInvitation", target_id__in=organization.invitations.filter(invited_by_id__in=other_moderators).values("pk"))
    )
    untargeted_by_another_moderator = Q(actor_id__in=other_moderators, target_id__isnull=True)
    return queryset.filter(Q(actor_id=scope.viewer.pk) | ~(about_hidden | untargeted_by_another_moderator))

"""Organization-scoping for views keyed by an ``organization_id`` URL kwarg.

Membership is re-resolved from PostgreSQL on every request -- never trusted
from client-supplied state -- per the isolation rule in
docs/features/27-31-46-moderator-organization-panel.md#5.1. A caller with no
visibility into the requested organization (wrong org, unrelated user) gets a
plain 404, matching that doc's access table; a caller who is a member but
lacks the required role gets 403.
"""
from django.http import Http404
from rest_framework.exceptions import PermissionDenied

from .models import OrganizationMembership
from .scope import DataScope
from .services import assert_organization_active, get_active_membership


class OrganizationScopedMixin:
    """Resolves ``request.active_membership``/``self.organization`` from the URL.

    Set ``require_owner_or_moderator = True`` on a view to additionally gate
    it to the organization's owner/moderator roles (403 for a plain member).
    """

    require_owner_or_moderator = False
    organization_url_kwarg = "organization_id"
    # Views that cover the WHOLE organization (the organization-wide export) are for the owner when moderators see only their own members.
    owner_only_when_scoped = False

    def initial(self, request, *args, **kwargs):
        self.perform_authentication(request)
        if request.user and request.user.is_authenticated:
            self.active_membership = get_active_membership(
                request.user, kwargs.get(self.organization_url_kwarg),
            )
        else:
            self.active_membership = None
        request.active_membership = self.active_membership
        super().initial(request, *args, **kwargs)
        if self.active_membership is None:
            raise Http404("Organization not found.")
        self.organization = self.active_membership.organization
        assert_organization_active(self.organization)          # after the 404 above: an outsider is never told the organization exists, suspended or not
        self.data_scope = DataScope(self.organization, request.user, self.active_membership)
        if self.owner_only_when_scoped and self.data_scope.restricted:
            raise PermissionDenied(
                "This covers the whole organization, and your organization lets moderators see only their own members: ask the organization owner."
            )
        if self.require_owner_or_moderator and self.active_membership.role not in (
            OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR,
        ):
            raise PermissionDenied("Organization owner or moderator access is required.")

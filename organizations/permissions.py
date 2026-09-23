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
from .services import get_active_membership


class OrganizationScopedMixin:
    """Resolves ``request.active_membership``/``self.organization`` from the URL.

    Set ``require_owner_or_moderator = True`` on a view to additionally gate
    it to the organization's owner/moderator roles (403 for a plain member).
    """

    require_owner_or_moderator = False
    organization_url_kwarg = "organization_id"

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
        if self.require_owner_or_moderator and self.active_membership.role not in (
            OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR,
        ):
            raise PermissionDenied("Organization owner or moderator access is required.")

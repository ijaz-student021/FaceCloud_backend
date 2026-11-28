"""A person whose membership in a SHARED organization is not active (suspended, removed, never accepted) cannot reach anything filed under that organization.

The rule, in one place
----------------------
Content filed under an organization (``UploadBatch.organization``, and everything that hangs off those batches: photographs, results, person folders, reports,
download requests, share links ...) is the organization's record while the person who uploaded it is one of its active members. When that membership is suspended
or removed, the person keeps their account, their personal workspace and every other organization, but every owner-scoped endpoint answers as if the organization's
content did not exist. Nothing is deleted or moved; the organization's moderators, its owner and the platform administrator keep seeing the records, and the content
comes back for the person the moment their membership is active again (``update_member`` applies the seat rules for that).

How it is enforced
------------------
* ``locked_org_ids()`` is the set of shared organizations in which **the person making the current request** has a membership that is not active. It is read from
  the database, lazily, once per request (no cache across requests, so a JWT issued before the suspension changes nothing).
* ``OrgLockedManager`` is the default manager of every model that holds organization-filed content. While that set is empty (everybody who is not locked out of
  something, every moderator view of somebody else's rows, every background task and management command) it adds nothing: no query, no join. When it is not
  empty it excludes every row whose organization is in the set.
* The set is installed per request by ``install_request_hook`` (a wrapper around DRF's ``APIView.initial``/``dispatch``, so it also covers ``force_authenticate``
  in tests). Outside a request (Celery workers, commands, the Django shell) there is no set and nothing is excluded.
* Public share links are anonymous, so they check the link owner explicitly (``owner_is_locked_for``).
"""
import contextvars
from functools import reduce
from operator import or_

from django.db.models import Q

_current = contextvars.ContextVar("facecloud_locked_orgs", default=None)


def locked_org_ids_for(user):
    """Ids of the shared organizations where ``user`` has a membership that is not active (read live from the database)."""
    from .models import OrganizationMembership

    if user is None or not getattr(user, "is_authenticated", False):
        return frozenset()
    return frozenset(
        OrganizationMembership.objects.filter(user=user, organization__is_personal=False)
        .exclude(status=OrganizationMembership.Status.ACTIVE)
        .values_list("organization_id", flat=True)
    )


class _LockedOrgs:
    """The current request's set, looked up on first use (a request that never touches filed content costs nothing)."""

    def __init__(self, user):
        self.user = user
        self._ids = None

    def ids(self):
        if self._ids is None:
            self._ids = locked_org_ids_for(self.user)
        return self._ids


def locked_org_ids():
    holder = _current.get()
    return holder.ids() if holder is not None else frozenset()


def member_can_access_org_content(user, organization_id):
    """True unless ``user`` has a non-active membership in that shared organization (a person who never belonged is handled by the callers' own checks)."""
    return organization_id not in locked_org_ids_for(user)


def owner_is_locked_for(owner, organization_id):
    """For anonymous paths (public share links): is ``owner`` locked out of the organization the content is filed under?"""
    return organization_id is not None and not member_can_access_org_content(owner, organization_id)


def exclude_locked(queryset, paths):
    """``queryset`` without the rows filed under an organization the current request's person is locked out of; unchanged when there is none."""
    ids = locked_org_ids()
    if not ids:
        return queryset
    # A sub-query on the primary key, not a join: the same excluded rows, and still valid under ``select_for_update`` on PostgreSQL (which refuses a lock across the nullable side of an outer join).
    locked_rows = queryset.model._base_manager.filter(reduce(or_, (Q(**{f"{path}__in": ids}) for path in paths))).values("pk")
    return queryset.exclude(pk__in=locked_rows)


class OrgLockedManager:
    """Mixin for a manager: ``lock_paths`` are the lookups that lead from a row to the id of the organization it is filed under."""

    lock_paths = ()

    def get_queryset(self):
        return exclude_locked(super().get_queryset(), self.lock_paths)


def locked_manager(*paths, base=None):
    """A default manager that hides what is filed under an organization the current person is locked out of."""
    from django.db import models

    base = base or models.Manager
    return type("OrgLocked" + base.__name__, (OrgLockedManager, base), {"lock_paths": tuple(paths)})()


def install_request_hook():
    """Set the current person's lock for the duration of each DRF request (idempotent)."""
    from rest_framework.views import APIView

    if getattr(APIView, "_facecloud_org_lock", False):
        return
    original_initial, original_dispatch = APIView.initial, APIView.dispatch

    def initial(self, request, *args, **kwargs):
        original_initial(self, request, *args, **kwargs)
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            _current.set(_LockedOrgs(user))

    def dispatch(self, request, *args, **kwargs):
        token = _current.set(None)
        try:
            return original_dispatch(self, request, *args, **kwargs)
        finally:
            _current.reset(token)

    APIView.initial, APIView.dispatch, APIView._facecloud_org_lock = initial, dispatch, True

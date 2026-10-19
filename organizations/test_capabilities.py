"""Advertised capabilities must match what the server actually allows.

Each capability is checked against the endpoint it describes, for an owner, a
moderator and a plain member: advertised exactly when the request is not
refused with 403.
"""
from django.test import TestCase

from .capabilities import CAPABILITY_NAMES
from .models import OrganizationMembership
from .tests import OrgFixtureMixin

Role = OrganizationMembership.Role


class CapabilitiesMatchEnforcementTests(OrgFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("owner")
        self.organization = self.make_org(self.owner)
        self.moderator = self.make_user("moderator")
        self.add_member(self.organization, self.moderator, Role.MODERATOR)
        self.member = self.make_user("member")
        self.add_member(self.organization, self.member, Role.MEMBER)
        self.target_member = self.make_user("targetmember")
        self.target = self.add_member(self.organization, self.target_member, Role.MEMBER)
        self.other_moderator_user = self.make_user("othermod")
        self.other_moderator = self.add_member(self.organization, self.other_moderator_user, Role.MODERATOR)

    def capabilities(self, user):
        response = self.authenticated_client(user).get(f"/api/organizations/{self.organization.pk}/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def allowed(self, user, method, path, data=None):
        client = self.authenticated_client(user)
        response = getattr(client, method)(f"/api/organizations/{self.organization.pk}/{path}", data or {}, format="json")
        return response.status_code != 403

    def probes(self):
        """capability -> (method, path, body) of a request that exercises it."""
        return {
            "view_members": ("get", "members/", None),
            "invite_members": ("post", "invitations/", {"email": "new-member@example.com", "role": "member"}),
            "invite_moderators": ("post", "invitations/", {"email": "new-mod@example.com", "role": "moderator"}),
            "manage_members": ("patch", f"members/{self.target.pk}/", {"status": "suspended", "reason": "test"}),
            "manage_moderators": ("patch", f"members/{self.other_moderator.pk}/", {"status": "suspended", "reason": "test"}),
            "grant_owner": ("patch", f"members/{self.target.pk}/", {"role": "owner"}),
            "view_audit": ("get", "audit/", None),
            "moderate_reviews": ("get", "moderator/reviews/", None),
            "manage_batches": ("get", "moderator/batches/", None),
            "create_reports": ("get", "moderator/reports/", None),
            "export_organization": ("get", "export/", None),
            "edit_organization": ("patch", "", {"name": "Acme"}),
        }

    def test_every_capability_is_probed(self):
        self.assertEqual(set(self.probes()), set(CAPABILITY_NAMES))

    def test_each_role_is_told_exactly_what_it_may_do(self):
        for user in (self.owner, self.moderator, self.member):
            body = self.capabilities(user)
            for name, (method, path, data) in self.probes().items():
                with self.subTest(user=user.username, capability=name):
                    # Reset what a previous probe changed.
                    OrganizationMembership.objects.filter(pk__in=[self.target.pk, self.other_moderator.pk]).update(
                        status=OrganizationMembership.Status.ACTIVE)
                    OrganizationMembership.objects.filter(pk=self.target.pk).update(role=Role.MEMBER)
                    self.organization.invitations.all().delete()
                    self.assertEqual(body["capabilities"][name], self.allowed(user, method, path, data))

    def test_the_callers_membership_is_included(self):
        body = self.capabilities(self.moderator)
        self.assertEqual((body["membership"]["role"], body["membership"]["status"]), ("moderator", "active"))

    def test_the_list_carries_each_organizations_own_membership(self):
        second = self.make_org(self.member, name="Second")
        response = self.authenticated_client(self.member).get("/api/organizations/")
        rows = response.json()
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[self.organization.pk]["membership"]["role"], "member")
        self.assertFalse(by_id[self.organization.pk]["capabilities"]["moderate_reviews"])
        self.assertEqual(by_id[second.pk]["membership"]["role"], "owner")
        self.assertTrue(by_id[second.pk]["capabilities"]["grant_owner"])

    def test_a_non_member_learns_nothing(self):
        outsider = self.make_user("outsider")
        response = self.authenticated_client(outsider).get(f"/api/organizations/{self.organization.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_a_suspended_member_is_not_listed(self):
        OrganizationMembership.objects.filter(user=self.member, organization=self.organization).update(
            status=OrganizationMembership.Status.SUSPENDED)
        rows = self.authenticated_client(self.member).get("/api/organizations/").json()
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertNotIn(self.organization.pk, [row["id"] for row in rows])

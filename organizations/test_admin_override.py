"""The administrator's exceptional ways of making somebody a moderator, and the privacy of personal workspaces.

The ordinary way: the Platform Admin creates the organization, its owner invites moderators, moderators invite members. The administrator keeps three ways around the owner (create a moderator
account, send an account invitation for one, promote an account); each needs a reason, is audited in the organization's own trail, and bypasses neither the plan's seats, nor an organization that
still waits for its owner, nor a personal workspace.
"""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from moderation.tests import ModerationFixtureMixin
from organizations.models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from organizations.services import create_organization, create_personal_organization, hash_token

User = get_user_model()
Role = OrganizationMembership.Role
REASON = "Pilot organization needs a moderator before its owner can invite one"


class Fixture(ModerationFixtureMixin):
    def setUp(self):
        self.admin = self.make_user("ov-admin", role="admin")
        self.admin_client = self.authenticated_client(self.admin)
        self.owner = self.make_user("ov-owner")
        self.organization = create_organization(name="Studio", owner=self.owner, plan="team")
        self.owner_client = self.authenticated_client(self.owner)

    def create_moderator(self, **extra):
        body = {"username": "ov-new", "email": "ov-new@example.com", "role": "moderator", "organization_id": self.organization.pk, **extra}
        return self.admin_client.post(reverse("admin-user-list"), body, format="json")

    def override_events(self, organization=None):
        return (organization or self.organization).audit_events.filter(action=OrganizationAuditEvent.Action.ADMIN_MODERATOR_OVERRIDE)

    def waiting_organization(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client.post(reverse("organizations:organization-admin-list-create"), {"name": "Waiting Co", "owner_email": "w@example.com", "plan": "team"}, format="json")
        return Organization.objects.get(pk=response.data["id"])


class AdministratorOverrideTests(Fixture, TestCase):
    def test_creating_a_moderator_account_needs_a_reason_and_creates_nothing_without_one(self):
        for extra in ({}, {"reason": ""}, {"reason": "ok"}, {"reason": "   "}):
            response = self.create_moderator(**extra)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, extra)
            self.assertIn("reason", response.data)
        self.assertFalse(User.objects.filter(username="ov-new").exists())
        self.assertEqual(self.override_events().count(), 0)

    def test_with_a_reason_the_account_is_created_and_the_override_is_in_the_organizations_audit_log(self):
        response = self.create_moderator(reason=f"  {REASON}  ")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        user = User.objects.get(username="ov-new")
        event = self.override_events().get()
        self.assertEqual((event.actor, event.reason, event.target_id), (self.admin, REASON, user.pk))
        self.assertEqual(event.new_value, {"operation": "create_account", "subject": "ov-new", "role": "moderator"})
        log = self.owner_client.get(reverse("organizations:organization-audit-list", kwargs={"organization_id": self.organization.pk}))
        rows = log.data["results"] if isinstance(log.data, dict) else log.data
        self.assertIn("admin_moderator_override", {row["action"] for row in rows})                       # the owner can read who put the moderator there, and why

    def test_an_account_invitation_for_a_moderator_needs_a_reason_and_audits_its_acceptance(self):
        url = "/api/user/admin/invitations/"
        body = {"email": "ov-invited@example.org", "role": "moderator", "organization_id": self.organization.pk}
        self.assertEqual(self.admin_client.post(url, body, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.admin_client.post(url, {**body, "reason": "no"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        with self.captureOnCommitCallbacks(execute=True):
            ok = self.admin_client.post(url, {**body, "reason": REASON}, format="json")
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED, ok.content)
        self.assertEqual(self.override_events().get().new_value["operation"], "account_invitation")
        token = re.search(r"token=([\w-]+)", mail.outbox[-1].body).group(1)
        accepted = self.client.post("/api/auth/invitations/accept/", {"token": token, "username": "ov-invited", "password": "A-long-unique-passphrase-7"}, format="json")
        self.assertEqual(accepted.status_code, status.HTTP_201_CREATED, accepted.content)
        arrival = self.organization.audit_events.filter(action=OrganizationAuditEvent.Action.INVITATION_ACCEPTED).latest("pk")
        self.assertEqual(arrival.new_value["via"], "administrator_invitation")
        self.assertTrue(OrganizationMembership.objects.filter(organization=self.organization, user__username="ov-invited", role="moderator", status="active").exists())

    def test_promoting_an_account_needs_a_reason_and_changes_nothing_without_one(self):
        person = self.make_user("ov-person")
        url = reverse("admin-user-update", kwargs={"pk": person.pk})
        refused = self.admin_client.patch(url, {"role": "moderator", "organization_id": self.organization.pk}, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        person.refresh_from_db()
        self.assertEqual(person.role, "user")
        self.assertFalse(OrganizationMembership.objects.filter(user=person, organization=self.organization).exists())
        done = self.admin_client.patch(url, {"role": "moderator", "organization_id": self.organization.pk, "reason": REASON}, format="json")
        self.assertEqual(done.status_code, status.HTTP_200_OK, done.content)
        event = self.override_events().get()
        self.assertEqual((event.new_value["operation"], event.reason, event.target_id), ("promotion", REASON, person.pk))

    def test_other_roles_and_other_changes_need_no_reason(self):
        plain = self.admin_client.post(reverse("admin-user-list"), {"username": "ov-plain", "email": "ov-plain@example.com", "role": "user"}, format="json")
        self.assertEqual(plain.status_code, status.HTTP_201_CREATED)
        person = self.make_user("ov-suspend")
        self.assertEqual(self.admin_client.patch(reverse("admin-user-update", kwargs={"pk": person.pk}), {"is_suspended": True, "suspension_reason": "policy"}, format="json").status_code, status.HTTP_200_OK)
        self.assertEqual(self.override_events().count(), 0)

    def test_the_ordinary_workflow_needs_no_override_the_owner_invites_the_moderator(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.owner_client.post(reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": self.organization.pk}), {"email": "ov-ordinary@example.com", "role": "moderator"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.override_events().count(), 0)

    def test_the_plan_still_limits_moderators_even_with_a_reason(self):
        free = create_organization(name="Free Co", owner=self.make_user("ov-free-owner"), plan="free")
        first = self.create_moderator(organization_id=free.pk, reason=REASON, username="ov-first", email="ov-first@example.com")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.create_moderator(organization_id=free.pk, reason=REASON, username="ov-second", email="ov-second@example.com")
        self.assertEqual((second.status_code, second.data["code"]), (status.HTTP_403_FORBIDDEN, "plan_limit_reached"))
        self.assertFalse(User.objects.filter(username="ov-second").exists())
        self.assertEqual(self.override_events(free).count(), 1)                                          # only the one that happened

    def test_an_organization_waiting_for_its_owner_cannot_be_given_a_moderator(self):
        waiting = self.waiting_organization()
        self.assertEqual(self.create_moderator(organization_id=waiting.pk, reason=REASON).status_code, status.HTTP_400_BAD_REQUEST)
        invitation = self.admin_client.post("/api/user/admin/invitations/", {"email": "ov-w@example.org", "role": "moderator", "organization_id": waiting.pk, "reason": REASON}, format="json")
        self.assertEqual(invitation.status_code, status.HTTP_400_BAD_REQUEST)
        person = self.make_user("ov-w-person")
        promote = self.admin_client.patch(reverse("admin-user-update", kwargs={"pk": person.pk}), {"role": "moderator", "organization_id": waiting.pk, "reason": REASON}, format="json")
        self.assertEqual(promote.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(waiting.memberships.count(), 0)
        self.assertEqual(waiting.status, "pending_owner")
        self.assertEqual(self.override_events(waiting).count(), 0)

    def test_a_personal_workspace_cannot_be_given_a_moderator(self):
        personal = create_personal_organization(self.make_user("ov-private"))
        self.assertEqual(self.create_moderator(organization_id=personal.pk, reason=REASON).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(personal.memberships.count(), 1)                                                  # only its own person

    def test_only_the_platform_administrator_can_use_these_operations(self):
        outsider = self.authenticated_client(self.make_user("ov-outsider"))
        for client in (outsider, self.owner_client):
            denied = client.post(reverse("admin-user-list"), {"username": "x1", "email": "x1@example.com", "role": "moderator", "organization_id": self.organization.pk, "reason": REASON}, format="json")
            self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.override_events().count(), 0)


class PersonalWorkspaceIsNeverSharedTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.person = self.make_user("ov-person-ws", email="ov-person-ws@example.com")
        self.workspace = create_personal_organization(self.person)
        self.person_client = self.authenticated_client(self.person)

    def invite_url(self):
        return reverse("organizations:organization-invitation-list-create", kwargs={"organization_id": self.workspace.pk})

    def test_nobody_can_be_invited_into_a_personal_workspace_by_its_owner(self):
        for role in ("member", "moderator"):
            response = self.person_client.post(self.invite_url(), {"email": "intruder@example.com", "role": role}, format="json")
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, role)
            self.assertEqual(response.data["code"], "personal_workspace_is_private")
        self.assertFalse(OrganizationInvitation.objects.filter(organization=self.workspace).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_personal_workspace_offers_no_management_capabilities(self):
        detail = self.person_client.get(reverse("organizations:organization-detail", kwargs={"organization_id": self.workspace.pk}))
        capabilities = detail.data["capabilities"]
        for name in ("invite_members", "invite_moderators", "manage_members", "manage_moderators", "grant_owner", "edit_organization"):
            self.assertFalse(capabilities[name], name)
        shared = self.owner_client.get(reverse("organizations:organization-detail", kwargs={"organization_id": self.organization.pk}))
        self.assertTrue(shared.data["capabilities"]["invite_members"])                                       # a shared organization's owner still can

    def test_an_invitation_that_somehow_points_at_a_personal_workspace_cannot_be_accepted(self):
        stranger = self.make_user("ov-stranger", email="ov-stranger@example.com")
        from datetime import timedelta

        from django.utils import timezone

        OrganizationInvitation.objects.create(
            organization=self.workspace, email="ov-stranger@example.com", role="member", token_hash=hash_token("planted"), invited_by=self.person,
            expires_at=timezone.now() + timedelta(hours=5),
        )
        response = self.authenticated_client(stranger).post(reverse("organizations:organization-invitation-accept"), {"token": "planted"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.workspace.memberships.count(), 1)

    def test_a_personal_workspace_is_not_managed_like_an_organization(self):
        membership = OrganizationMembership.objects.get(organization=self.workspace, user=self.person)
        response = self.person_client.patch(reverse("organizations:organization-member-detail", kwargs={"organization_id": self.workspace.pk, "membership_id": membership.pk}), {"role": "moderator"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        edit = self.person_client.patch(reverse("organizations:organization-detail", kwargs={"organization_id": self.workspace.pk}), {"name": "Mine"}, format="json")
        self.assertEqual(edit.status_code, status.HTTP_403_FORBIDDEN)
        admin_patch = self.admin_client.patch(reverse("organizations:organization-admin-detail", kwargs={"organization_id": self.workspace.pk}), {"status": "suspended"}, format="json")
        self.assertEqual(admin_patch.status_code, status.HTTP_400_BAD_REQUEST)                                  # not even the administrator suspends a private workspace
        self.assertEqual(self.admin_client.get(reverse("organizations:organization-admin-owner-invitation", kwargs={"organization_id": self.workspace.pk})).status_code, status.HTTP_404_NOT_FOUND)

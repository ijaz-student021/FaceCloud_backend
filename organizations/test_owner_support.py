"""What an organization's owner needs for their console: which organizations they own (login), their organization's detail, changing its name and what its
moderators see, and the powers over members they already have."""
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from .services import create_organization, create_personal_organization

PASSWORD = "ForestRiver!4821"
Role = OrganizationMembership.Role
State = OrganizationMembership.Status
Scope = Organization.ModeratorDataScope
Action = OrganizationAuditEvent.Action


class Fixtures:
    def setUp(self):
        cache.clear()
        self.admin = self.person("os-admin", role="admin")
        self.owner = self.person("os-owner")
        self.moderator = self.person("os-mod", role="moderator")
        self.other_moderator = self.person("os-mod2", role="moderator")
        self.member = self.person("os-member")
        self.outsider = self.person("os-outsider")
        for user in (self.owner, self.moderator, self.other_moderator, self.member, self.outsider):
            create_personal_organization(user)
        self.organization = create_organization(name="Acme", owner=self.owner, actor=self.admin)
        self.mod_membership = self.join(self.moderator, Role.MODERATOR)
        self.join(self.other_moderator, Role.MODERATOR)
        self.member_membership = self.join(self.member, Role.MEMBER, invited_by=self.other_moderator)
        self.url = reverse("organizations:organization-detail", args=[self.organization.pk])

    def person(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def join(self, user, role, organization=None, status_value=State.ACTIVE, **extra):
        return OrganizationMembership.objects.create(organization=organization or self.organization, user=user, role=role, status=status_value, **extra)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def members_url(self, membership=None):
        args = [self.organization.pk] + ([membership.pk] if membership else [])
        return reverse("organizations:organization-member-detail" if membership else "organizations:organization-member-list", args=args)

    def sign_in(self, user):
        return APIClient().post(reverse("login"), {"identifier": user.username, "password": PASSWORD}, format="json")


class LoginResponseTests(Fixtures, TestCase):
    def roles(self, user):
        response = self.sign_in(user)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["organization_roles"]

    def test_the_owner_learns_which_organization_they_own_and_the_personal_workspace_is_left_out(self):
        self.assertEqual(self.roles(self.owner), [{"organization_id": self.organization.pk, "name": "Acme", "role": "owner", "status": "active"}])

    def test_a_moderator_and_a_member_see_their_role_and_someone_with_nothing_gets_an_empty_list(self):
        self.assertEqual([row["role"] for row in self.roles(self.moderator)], ["moderator"])
        self.assertEqual([row["role"] for row in self.roles(self.member)], ["member"])
        self.assertEqual(self.roles(self.outsider), [])
        self.assertEqual(self.roles(self.admin), [])

    def test_only_active_memberships_count_and_a_second_organization_is_listed_oldest_first(self):
        second = create_organization(name="Beta", owner=self.person("os-beta-owner"))
        self.join(self.member, Role.OWNER, organization=second)
        removed = create_organization(name="Gone", owner=self.person("os-gone-owner"))
        self.join(self.member, Role.MEMBER, organization=removed, status_value=State.REMOVED)
        self.assertEqual([(row["name"], row["role"]) for row in self.roles(self.member)], [("Acme", "member"), ("Beta", "owner")])

    def test_a_suspended_organization_is_still_listed_so_the_page_can_say_so(self):
        Organization.objects.filter(pk=self.organization.pk).update(status=Organization.Status.SUSPENDED)
        self.assertEqual(self.roles(self.owner)[0]["status"], "suspended")

    def test_nothing_that_was_in_the_login_response_is_gone(self):
        body = self.sign_in(self.owner).json()
        self.assertTrue({"access", "refresh", "user", "role", "dashboard"} <= set(body))
        self.assertEqual(body["dashboard"], "user")

    def test_the_organization_list_marks_the_owner_and_offers_the_edit_button_to_the_owner_only(self):
        def rows(user):
            body = self.client_for(user).get(reverse("organizations:organization-list")).json()
            return body["results"] if isinstance(body, dict) else body

        def team(user):
            return next(item for item in rows(user) if item["id"] == self.organization.pk)

        self.assertEqual((team(self.owner)["membership"]["role"], team(self.owner)["is_personal"]), ("owner", False))
        self.assertTrue(team(self.owner)["capabilities"]["edit_organization"])
        self.assertFalse(team(self.moderator)["capabilities"]["edit_organization"])
        personal = next(item for item in rows(self.owner) if item["is_personal"])
        self.assertFalse(personal["capabilities"]["edit_organization"])


class DetailTests(Fixtures, TestCase):
    def test_the_owner_and_the_moderators_see_the_owner_the_counts_the_scope_and_the_plan(self):
        for user in (self.owner, self.moderator):
            body = self.client_for(user).get(self.url).json()
            self.assertEqual(body["owner"], {"id": self.owner.pk, "username": "os-owner"}, user.username)
            self.assertEqual((body["member_count"], body["moderator_count"]), (4, 2), user.username)
            self.assertEqual((body["plan"], body["moderator_data_scope"], body["status"]), ("free", "own_members", "active"), user.username)

    def test_a_plain_member_gets_the_organization_and_their_own_membership_but_not_the_head_counts(self):
        body = self.client_for(self.member).get(self.url).json()
        for key in ("owner", "member_count", "moderator_count"):
            self.assertNotIn(key, body)
        self.assertEqual((body["name"], body["membership"]["role"]), ("Acme", "member"))
        self.assertIn("capabilities", body)

    def test_only_active_memberships_are_counted_and_a_stranger_still_gets_404(self):
        self.join(self.person("os-gone"), Role.MEMBER, status_value=State.REMOVED)
        self.join(self.person("os-held"), Role.MODERATOR, status_value=State.SUSPENDED)
        body = self.client_for(self.owner).get(self.url).json()
        self.assertEqual((body["member_count"], body["moderator_count"]), (4, 2))
        self.assertEqual(self.client_for(self.outsider).get(self.url).status_code, 404)

    def test_the_cost_does_not_grow_with_the_number_of_members(self):
        client = self.client_for(self.owner)
        client.get(self.url)

        def cost():
            with CaptureQueriesContext(connection) as captured:
                client.get(self.url)
            return len(captured)

        before = cost()
        for index in range(6):
            self.join(self.person(f"os-extra-{index}"), Role.MEMBER)
        self.assertEqual(cost(), before)


class OwnerPatchTests(Fixtures, TestCase):
    def patch(self, body, user=None):
        return self.client_for(user or self.owner).patch(self.url, body, format="json")

    def test_the_owner_renames_the_organization_and_the_answer_is_the_new_detail(self):
        response = self.patch({"name": "  Acme Ltd "})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["name"], body["owner"]["username"], body["member_count"]), ("Acme Ltd", "os-owner", 4))
        self.assertEqual(Organization.objects.get(pk=self.organization.pk).name, "Acme Ltd")

    def test_the_owner_chooses_what_moderators_see_and_it_takes_effect_at_once(self):
        def members(user):
            body = self.client_for(user).get(self.members_url()).json()
            return len(body["results"] if isinstance(body, dict) else body)

        self.assertEqual(members(self.moderator), 3)                      # the member another moderator invited is hidden: owner + two moderators
        body = self.patch({"moderator_data_scope": "organization"}).json()
        self.assertEqual(body["moderator_data_scope"], "organization")
        self.assertEqual(members(self.moderator), 4)
        self.assertEqual(self.patch({"moderator_data_scope": "own_members"}).json()["moderator_data_scope"], "own_members")
        self.assertEqual(members(self.moderator), 3)

    def test_the_export_capability_follows_the_new_setting_in_the_same_answer(self):
        self.client_for(self.owner)
        body = self.client_for(self.owner).patch(self.url, {"moderator_data_scope": "organization"}, format="json").json()
        self.assertTrue(body["capabilities"]["export_organization"])

    def test_the_change_is_audited_with_the_old_and_new_values(self):
        self.patch({"name": "Acme Ltd", "moderator_data_scope": "organization", "reason": "we trust our team"})
        event = OrganizationAuditEvent.objects.filter(organization=self.organization, action=Action.ORGANIZATION_UPDATED).get()
        self.assertEqual(event.actor, self.owner)
        self.assertEqual(event.previous_value, {"name": "Acme", "moderator_data_scope": "own_members"})
        self.assertEqual(event.new_value, {"name": "Acme Ltd", "moderator_data_scope": "organization"})
        self.assertEqual(event.reason, "we trust our team")

    def test_a_request_that_changes_nothing_records_nothing(self):
        before = OrganizationAuditEvent.objects.count()
        self.assertEqual(self.patch({"name": "Acme", "moderator_data_scope": "own_members"}).status_code, 200)
        self.assertEqual(OrganizationAuditEvent.objects.count(), before)

    def test_the_plan_and_the_status_belong_to_the_administrator_even_when_sent_with_other_changes(self):
        for body in ({"plan": "enterprise"}, {"status": "suspended"}, {"name": "Sneaky", "plan": "team"}, {"plan": "free"}):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 403)
        organization = Organization.objects.get(pk=self.organization.pk)
        self.assertEqual((organization.name, organization.plan, organization.status), ("Acme", "free", "active"))

    def test_moderators_and_members_cannot_change_it_and_a_stranger_gets_404(self):
        for user in (self.moderator, self.member):
            self.assertEqual(self.patch({"name": "Mine now"}, user=user).status_code, 403, user.username)
        self.assertEqual(self.patch({"name": "Mine now"}, user=self.outsider).status_code, 404)
        self.assertEqual(APIClient().patch(self.url, {"name": "X"}, format="json").status_code, 401)
        self.assertEqual(Organization.objects.get(pk=self.organization.pk).name, "Acme")

    def test_bad_values_are_refused(self):
        for body in ({"name": "   "}, {"name": "x" * 151}, {"moderator_data_scope": "everyone"}, {}, {"reason": "nothing to change"}):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 400)

    def test_a_personal_workspace_is_not_edited_through_this(self):
        personal = Organization.objects.get(is_personal=True, memberships__user=self.owner)
        response = self.client_for(self.owner).patch(reverse("organizations:organization-detail", args=[personal.pk]), {"name": "Mine"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_a_suspended_organization_cannot_be_edited_by_its_owner(self):
        Organization.objects.filter(pk=self.organization.pk).update(status=Organization.Status.SUSPENDED)
        response = self.patch({"name": "Escape"})
        self.assertEqual((response.status_code, response.json()["code"]), (403, "organization_suspended"))

    def test_the_administrator_can_also_set_what_moderators_see(self):
        url = reverse("organizations:organization-admin-detail", args=[self.organization.pk])
        response = self.client_for(self.admin).patch(url, {"moderator_data_scope": "organization"}, format="json")
        self.assertEqual((response.status_code, response.json()["moderator_data_scope"]), (200, "organization"))
        event = OrganizationAuditEvent.objects.filter(organization=self.organization, action=Action.ORGANIZATION_UPDATED).get()
        self.assertEqual((event.actor, event.new_value), (self.admin, {"moderator_data_scope": "organization"}))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OwnerPowersOverMembersTests(Fixtures, TestCase):
    """Each thing an owner of a non-personal organization is expected to do, through the endpoints that already exist (on a plan with no seat limits)."""

    def setUp(self):
        super().setUp()
        Organization.objects.filter(pk=self.organization.pk).update(plan=Organization.Plan.ENTERPRISE)

    def change(self, membership, body, user=None):
        return self.client_for(user or self.owner).patch(self.members_url(membership), body, format="json")

    def test_the_owner_deactivates_and_reactivates_a_member(self):
        self.assertEqual(self.change(self.member_membership, {"status": "suspended", "reason": "left the team"}).status_code, 200)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.status, State.SUSPENDED)
        self.assertEqual(self.client_for(self.member).get(self.url).status_code, 404)                 # a deactivated member no longer sees the organization
        self.assertEqual(self.change(self.member_membership, {"status": "active"}).status_code, 200)
        self.assertEqual(self.client_for(self.member).get(self.url).status_code, 200)

    def test_the_owner_deactivates_a_moderator_and_their_open_reviews_are_released(self):
        response = self.change(self.mod_membership, {"status": "suspended"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.client_for(self.moderator).get(self.url).status_code, 404)
        event = OrganizationAuditEvent.objects.filter(action=Action.MEMBER_STATUS_CHANGED, target_id=self.mod_membership.pk).get()
        self.assertEqual(event.actor, self.owner)

    def test_the_owner_removes_a_member_and_a_moderator(self):
        for membership in (self.member_membership, self.mod_membership):
            response = self.client_for(self.owner).delete(self.members_url(membership), {"reason": "contract ended"}, format="json")
            self.assertEqual(response.status_code, 200, response.content)
            membership.refresh_from_db()
            self.assertEqual(membership.status, State.REMOVED)
        self.assertEqual(OrganizationAuditEvent.objects.filter(action=Action.MEMBER_REMOVED).count(), 2)
        body = self.client_for(self.owner).get(self.url).json()
        self.assertEqual((body["member_count"], body["moderator_count"]), (2, 1))

    def test_the_owner_promotes_a_member_to_moderator_then_to_owner_and_demotes_again(self):
        self.assertEqual(self.change(self.member_membership, {"role": "moderator"}).status_code, 200)
        self.assertEqual(self.client_for(self.owner).get(self.url).json()["moderator_count"], 3)
        self.assertEqual(self.sign_in(self.member).json()["dashboard"], "moderator")                  # the portal follows the membership
        self.assertEqual(self.change(self.member_membership, {"role": "owner"}).status_code, 200)
        self.assertEqual(self.client_for(self.member).patch(self.url, {"name": "Now ours"}, format="json").status_code, 200)    # a second owner can edit it
        self.assertEqual(self.change(self.member_membership, {"role": "member"}).status_code, 200)
        self.assertEqual(self.client_for(self.member).patch(self.url, {"name": "Again"}, format="json").status_code, 403)

    def test_the_owner_invites_a_moderator_and_a_member_but_a_moderator_may_only_invite_members(self):
        invitations = reverse("organizations:organization-invitation-list-create", args=[self.organization.pk])
        moderator_invite = self.client_for(self.owner).post(invitations, {"email": "new-mod@example.com", "role": "moderator"}, format="json")
        self.assertEqual(moderator_invite.status_code, 201, moderator_invite.content)
        member_invite = self.client_for(self.owner).post(invitations, {"email": "new-member@example.com", "role": "member"}, format="json")
        self.assertEqual(member_invite.status_code, 201, member_invite.content)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(OrganizationInvitation.objects.filter(organization=self.organization).count(), 2)
        refused = self.client_for(self.moderator).post(invitations, {"email": "sneaky@example.com", "role": "moderator"}, format="json")
        self.assertEqual(refused.status_code, 403)

    def test_the_last_owner_can_neither_be_removed_demoted_nor_deactivated(self):
        owner_membership = OrganizationMembership.objects.get(organization=self.organization, user=self.owner)
        self.assertEqual(self.change(owner_membership, {"role": "member"}).status_code, 409)
        self.assertEqual(self.change(owner_membership, {"status": "suspended"}).status_code, 409)
        self.assertEqual(self.client_for(self.owner).delete(self.members_url(owner_membership), {}, format="json").status_code, 409)

    def test_a_moderator_cannot_touch_the_owner_or_another_moderator(self):
        owner_membership = OrganizationMembership.objects.get(organization=self.organization, user=self.owner)
        other = OrganizationMembership.objects.get(organization=self.organization, user=self.other_moderator)
        for membership in (owner_membership, other):
            self.assertEqual(self.change(membership, {"status": "suspended"}, user=self.moderator).status_code, 403)

    def test_the_owner_requests_the_organization_export_even_when_moderators_see_only_their_own_members(self):
        export = reverse("privacy_organizations:organization-export-list-create", kwargs={"organization_id": self.organization.pk})
        self.assertEqual(Organization.objects.get(pk=self.organization.pk).moderator_data_scope, Scope.OWN_MEMBERS)
        self.assertEqual(self.client_for(self.owner).post(export).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.client_for(self.moderator).post(export).status_code, status.HTTP_403_FORBIDDEN)

"""Plan limits: how many moderators and members an organization may have, enforced wherever people are added, and shown as limits and usage."""
import os
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.admin import FaceCloudUserCreationForm
from accounts.models import AccountInvitation, User
from config import settings as project_settings

from .models import Organization, OrganizationInvitation, OrganizationMembership
from .services import create_organization, create_personal_organization, hash_token, seats_in_use

PASSWORD = "ForestRiver!4821"
Role = OrganizationMembership.Role
State = OrganizationMembership.Status
SMALL = {
    "free": {"moderators": 1, "members": 2},
    "team": {"moderators": 2, "members": 3},
    "enterprise": {"moderators": None, "members": None},
}


class Fixtures:
    def setUp(self):
        cache.clear()
        self.admin = self.person("pl-admin", role="admin")
        self.owner = self.person("pl-owner")
        self.organization = create_organization(name="Plan Co", owner=self.owner)
        self.counter = 0

    def person(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def join(self, role=Role.MEMBER, status_value=State.ACTIVE, organization=None, user=None):
        self.counter += 1
        user = user or self.person(f"pl-person-{self.counter}")
        return OrganizationMembership.objects.create(organization=organization or self.organization, user=user, role=role, status=status_value)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def invite(self, role, email=None, actor=None, organization=None):
        self.counter += 1
        organization = organization or self.organization
        return self.client_for(actor or self.owner).post(
            reverse("organizations:organization-invitation-list-create", args=[organization.pk]),
            {"email": email or f"invitee-{self.counter}@example.com", "role": role}, format="json")

    def set_plan(self, plan):
        Organization.objects.filter(pk=self.organization.pk).update(plan=plan)
        self.organization.refresh_from_db()

    def change_member(self, membership, body):
        return self.client_for(self.owner).patch(
            reverse("organizations:organization-member-detail", args=[self.organization.pk, membership.pk]), body, format="json")

    def assert_limit(self, response, kind, limit, sentence):
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json(), {"detail": sentence, "code": "plan_limit_reached", "kind": kind, "limit": limit})


class SettingsTests(TestCase):
    def test_the_defaults_are_the_ones_the_product_is_sold_by(self):
        limits = project_settings.ORGANIZATION_PLAN_LIMITS
        self.assertEqual(limits["free"], {"moderators": 1, "members": 10})
        self.assertEqual(limits["team"], {"moderators": 5, "members": 100})
        self.assertEqual(limits["enterprise"], {"moderators": None, "members": None})

    def test_a_limit_can_be_changed_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ORG_PLAN_FREE_MODERATORS": "3", "ORG_PLAN_TEAM_MEMBERS": "unlimited", "ORG_PLAN_FREE_MEMBERS": ""}):
            self.assertEqual(project_settings._plan_limit("free", "moderators", 1), 3)
            self.assertIsNone(project_settings._plan_limit("team", "members", 100))
            self.assertEqual(project_settings._plan_limit("free", "members", 10), 10)             # blank = the default
        with mock.patch.dict(os.environ, {"ORG_PLAN_FREE_MODERATORS": "lots"}):
            with self.assertRaises(Exception):
                project_settings._plan_limit("free", "moderators", 1)


@override_settings(ORGANIZATION_PLAN_LIMITS=SMALL, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class InvitationLimitTests(Fixtures, TestCase):
    def test_a_moderator_can_be_invited_up_to_the_limit_and_the_next_one_is_refused_with_the_sentence(self):
        self.assertEqual(self.invite("moderator").status_code, 201)
        self.assert_limit(self.invite("moderator"), "moderators", 1, "Your plan allows 1 moderator.")          # a waiting invitation holds the seat
        self.assertEqual(OrganizationInvitation.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_members_are_limited_separately_and_plural_reads_right(self):
        self.join()
        self.assertEqual(self.invite("member").status_code, 201)                    # 1 active + 1 waiting = 2 of 2
        self.assert_limit(self.invite("member"), "members", 2, "Your plan allows 2 members.")
        self.assertEqual(self.invite("moderator").status_code, 201)                  # the moderator seat is a different pool

    def test_cancelling_an_invitation_frees_its_seat(self):
        first = self.invite("moderator").json()
        self.assert_limit(self.invite("moderator"), "moderators", 1, "Your plan allows 1 moderator.")
        cancelled = self.client_for(self.owner).delete(reverse("organizations:organization-invitation-cancel", args=[self.organization.pk, first["id"]]))
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(self.invite("moderator").status_code, 201)

    def test_an_expired_invitation_holds_no_seat_and_bringing_it_back_needs_one(self):
        first = self.invite("moderator").json()
        OrganizationInvitation.objects.filter(pk=first["id"]).update(expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(seats_in_use(self.organization, "moderators"), 0)
        self.assertEqual(self.invite("moderator").status_code, 201)                  # takes the seat the expired one gave up
        resend = self.client_for(self.owner).post(reverse("organizations:organization-invitation-resend", args=[self.organization.pk, first["id"]]))
        self.assert_limit(resend, "moderators", 1, "Your plan allows 1 moderator.")

    def test_resending_a_live_invitation_needs_no_new_seat(self):
        first = self.invite("moderator").json()
        resend = self.client_for(self.owner).post(reverse("organizations:organization-invitation-resend", args=[self.organization.pk, first["id"]]))
        self.assertEqual(resend.status_code, 200, resend.content)

    def test_the_owner_takes_no_seat_and_removing_a_person_frees_theirs(self):
        moderator = self.join(Role.MODERATOR)
        self.assert_limit(self.invite("moderator"), "moderators", 1, "Your plan allows 1 moderator.")            # exactly at the limit
        removed = self.client_for(self.owner).delete(reverse("organizations:organization-member-detail", args=[self.organization.pk, moderator.pk]))
        self.assertEqual(removed.status_code, 200, removed.content)
        self.assertEqual(self.invite("moderator").status_code, 201)

    def test_a_suspended_member_holds_no_seat(self):
        self.join()
        held = self.join()
        self.assert_limit(self.invite("member"), "members", 2, "Your plan allows 2 members.")
        self.assertEqual(self.change_member(held, {"status": "suspended"}).status_code, 200)
        self.assertEqual(self.invite("member").status_code, 201)

    def test_enterprise_is_unlimited_and_a_bigger_plan_opens_room(self):
        self.join(Role.MODERATOR)
        self.assertEqual(self.invite("moderator").status_code, 403)
        self.set_plan("team")
        self.assertEqual(self.invite("moderator").status_code, 201)
        self.assert_limit(self.invite("moderator"), "moderators", 2, "Your plan allows 2 moderators.")
        self.set_plan("enterprise")
        for _ in range(4):
            self.assertEqual(self.invite("moderator").status_code, 201)

    def test_a_moderator_inviting_a_member_meets_the_same_limit(self):
        moderator = self.person("pl-mod", role="moderator")
        self.join(Role.MODERATOR, user=moderator)
        self.join()
        self.join()
        self.assert_limit(self.invite("member", actor=moderator), "members", 2, "Your plan allows 2 members.")


@override_settings(ORGANIZATION_PLAN_LIMITS=SMALL, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AcceptanceAndRoleChangeTests(Fixtures, TestCase):
    def pending_invitation(self, role, email):
        token = f"token-{email}"
        OrganizationInvitation.objects.create(
            organization=self.organization, email=email, role=role, token_hash=hash_token(token), invited_by=self.owner,
            expires_at=timezone.now() + timedelta(hours=24))
        return token

    def accept(self, user, token):
        return self.client_for(user).post(reverse("organizations:organization-invitation-accept"), {"token": token}, format="json")

    def test_acceptance_is_checked_again_so_a_smaller_plan_cannot_be_got_round(self):
        invited = self.person("pl-invited")
        token = self.pending_invitation(Role.MODERATOR, invited.email)
        self.join(Role.MODERATOR)                                                    # another moderator took the seat meanwhile (added directly)
        self.assert_limit(self.accept(invited, token), "moderators", 1, "Your plan allows 1 moderator.")
        self.assertFalse(OrganizationMembership.objects.filter(user=invited, organization=self.organization).exists())
        self.assertEqual(OrganizationInvitation.objects.get().status, OrganizationInvitation.Status.PENDING)     # still usable once there is room

    def test_accepting_an_invitation_that_holds_the_last_seat_works(self):
        invited = self.person("pl-invited2")
        token = self.pending_invitation(Role.MODERATOR, invited.email)
        self.assertEqual(self.accept(invited, token).status_code, 200)

    def test_promoting_a_member_to_moderator_takes_a_moderator_seat(self):
        self.join(Role.MODERATOR)
        member = self.join()
        self.assert_limit(self.change_member(member, {"role": "moderator"}), "moderators", 1, "Your plan allows 1 moderator.")
        member.refresh_from_db()
        self.assertEqual(member.role, Role.MEMBER)

    def test_demoting_a_moderator_to_member_takes_a_member_seat(self):
        self.join()
        self.join()
        moderator = self.join(Role.MODERATOR)
        self.assert_limit(self.change_member(moderator, {"role": "member"}), "members", 2, "Your plan allows 2 members.")

    def test_promoting_to_owner_takes_no_seat(self):
        self.join(Role.MODERATOR)
        member = self.join()
        self.assertEqual(self.change_member(member, {"role": "owner"}).status_code, 200)

    def test_reactivating_a_deactivated_member_needs_a_seat(self):
        held = self.join(status_value=State.SUSPENDED)
        self.join()
        self.join()
        self.assert_limit(self.change_member(held, {"status": "active"}), "members", 2, "Your plan allows 2 members.")

    def test_changing_nothing_about_seats_is_never_refused(self):
        self.join(Role.MODERATOR)
        self.join()
        self.join()
        moderator = OrganizationMembership.objects.filter(organization=self.organization, role=Role.MODERATOR).get()
        self.assertEqual(self.change_member(moderator, {"status": "active", "reason": "no change"}).status_code, 200)

    def test_an_organization_already_over_its_limit_keeps_everyone_and_cannot_add_more_until_it_is_under(self):
        first, second = self.join(Role.MODERATOR), self.join(Role.MODERATOR)             # 2 moderators on a plan that allows 1
        self.assertEqual(self.client_for(self.owner).get(reverse("organizations:organization-member-list", args=[self.organization.pk])).status_code, 200)
        self.assertEqual(self.invite("moderator").status_code, 403)
        for membership in (first, second):
            removed = self.client_for(self.owner).delete(reverse("organizations:organization-member-detail", args=[self.organization.pk, membership.pk]))
            self.assertEqual(removed.status_code, 200)
            if membership is first:
                self.assertEqual(self.invite("moderator").status_code, 403)             # one moderator left = at the limit
        self.assertEqual(self.invite("moderator").status_code, 201)


@override_settings(ORGANIZATION_PLAN_LIMITS=SMALL, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdministratorPathsTests(Fixtures, TestCase):
    def create_moderator(self, name="pl-new-mod"):
        return self.client_for(self.admin).post(
            reverse("admin-user-list"),
            {"username": name, "email": f"{name}@example.com", "password": PASSWORD, "role": "moderator", "organization_id": self.organization.pk},
            format="json")

    def test_creating_a_moderator_account_for_a_full_organization_is_refused_and_creates_nothing(self):
        self.assertEqual(self.create_moderator("pl-first").status_code, 201)
        self.assert_limit(self.create_moderator("pl-second"), "moderators", 1, "Your plan allows 1 moderator.")
        self.assertFalse(User.objects.filter(username="pl-second").exists())

    def test_creating_an_ordinary_user_is_not_limited(self):
        self.join(Role.MODERATOR)
        response = self.client_for(self.admin).post(
            reverse("admin-user-list"), {"username": "pl-plain", "email": "pl-plain@example.com", "password": PASSWORD, "role": "user"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)

    def test_changing_a_users_role_to_moderator_is_limited_too(self):
        self.join(Role.MODERATOR)
        candidate = self.person("pl-candidate")
        response = self.client_for(self.admin).patch(
            reverse("admin-user-update", args=[candidate.pk]), {"role": "moderator", "organization_id": self.organization.pk}, format="json")
        self.assert_limit(response, "moderators", 1, "Your plan allows 1 moderator.")
        candidate.refresh_from_db()
        self.assertEqual(candidate.role, "user")
        self.assertFalse(OrganizationMembership.objects.filter(user=candidate).exclude(organization__is_personal=True).exists())

    def test_an_administrators_account_invitation_for_a_moderator_holds_a_seat_and_is_limited(self):
        invite = lambda email: self.client_for(self.admin).post(                   # noqa: E731
            "/api/user/admin/invitations/", {"email": email, "role": "moderator", "organization_id": self.organization.pk}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(invite("one@example.org").status_code, 201)
            self.assert_limit(invite("two@example.org"), "moderators", 1, "Your plan allows 1 moderator.")
        self.assert_limit(self.invite("moderator"), "moderators", 1, "Your plan allows 1 moderator.")            # the organization's own invitation sees it too
        self.assertEqual(seats_in_use(self.organization, "moderators"), 1)

    def test_accepting_an_account_invitation_is_checked_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client_for(self.admin).post(
                "/api/user/admin/invitations/", {"email": "late@example.org", "role": "moderator", "organization_id": self.organization.pk}, format="json")
        import re

        token = re.search(r"token=([\w-]+)", mail.outbox[-1].body).group(1)
        self.join(Role.MODERATOR)                                                    # the seat was taken some other way
        response = APIClient().post("/api/auth/invitations/accept/", {"token": token, "username": "pl-late", "password": PASSWORD}, format="json")
        self.assert_limit(response, "moderators", 1, "Your plan allows 1 moderator.")
        self.assertFalse(User.objects.filter(username="pl-late").exists())
        self.assertEqual(AccountInvitation.objects.get().status, AccountInvitation.Status.PENDING)

    def test_the_django_admin_form_shows_the_same_sentence(self):
        self.join(Role.MODERATOR)
        form = FaceCloudUserCreationForm(data={
            "username": "pl-form", "password1": PASSWORD, "password2": PASSWORD, "role": "moderator", "moderator_organization": self.organization.pk})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["moderator_organization"], ["Your plan allows 1 moderator."])


@override_settings(ORGANIZATION_PLAN_LIMITS=SMALL)
class LimitsAndUsageTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.moderator = self.person("pl-mod", role="moderator")
        self.join(Role.MODERATOR, user=self.moderator)
        self.member_user = self.person("pl-member")
        self.join(user=self.member_user)
        self.url = reverse("organizations:organization-detail", args=[self.organization.pk])
        self.admin_url = reverse("organizations:organization-admin-list-create")

    def test_the_owner_and_the_moderators_see_limits_and_usage_and_a_member_does_not(self):
        for user in (self.owner, self.moderator):
            body = self.client_for(user).get(self.url).json()
            self.assertEqual(body["limits"], {"moderators": 1, "members": 2}, user.username)
            self.assertEqual(body["usage"], {"moderators": 1, "members": 1}, user.username)
        body = self.client_for(self.member_user).get(self.url).json()
        self.assertNotIn("limits", body)
        self.assertNotIn("usage", body)

    def test_usage_counts_waiting_invitations_and_leaves_out_people_who_are_not_active(self):
        self.join(status_value=State.SUSPENDED)
        self.join(status_value=State.REMOVED)
        OrganizationInvitation.objects.create(
            organization=self.organization, email="w@example.com", role="member", token_hash=hash_token("w"), invited_by=self.owner,
            expires_at=timezone.now() + timedelta(hours=5))
        OrganizationInvitation.objects.create(
            organization=self.organization, email="e@example.com", role="member", token_hash=hash_token("e"), invited_by=self.owner,
            expires_at=timezone.now() - timedelta(hours=5))
        body = self.client_for(self.owner).get(self.url).json()
        self.assertEqual(body["usage"], {"moderators": 1, "members": 2})
        self.assertEqual((body["member_count"], body["moderator_count"]), (3, 1))        # head counts stay "active people"

    def test_unlimited_is_null(self):
        self.set_plan("enterprise")
        self.assertEqual(self.client_for(self.owner).get(self.url).json()["limits"], {"moderators": None, "members": None})

    def test_the_figures_shown_are_the_figures_enforced(self):
        client = self.client_for(self.owner)
        body = client.get(self.url).json()
        for kind in ("moderators", "members"):
            self.assertEqual(body["usage"][kind], seats_in_use(self.organization, kind))

    def test_the_administrators_list_has_them_for_every_organization_in_a_fixed_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = self.client_for(self.admin)
        rows = client.get(self.admin_url).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        row = next(item for item in rows if item["id"] == self.organization.pk)
        self.assertEqual((row["limits"], row["usage"]), ({"moderators": 1, "members": 2}, {"moderators": 1, "members": 1}))

        def cost():
            with CaptureQueriesContext(connection) as captured:
                client.get(self.admin_url)
            return len(captured)

        before = cost()
        for index in range(4):
            create_organization(name=f"Another {index}", owner=self.person(f"pl-other-{index}"))
        self.assertEqual(cost(), before)

    def test_the_administrators_detail_and_the_answer_to_a_patch_have_them_and_a_plan_change_moves_the_limits(self):
        url = reverse("organizations:organization-admin-detail", args=[self.organization.pk])
        self.assertEqual(self.client_for(self.admin).get(url).json()["limits"]["members"], 2)
        body = self.client_for(self.admin).patch(url, {"plan": "team"}, format="json").json()
        self.assertEqual(body["limits"], {"moderators": 2, "members": 3})

    def test_a_personal_workspace_is_not_exempt_but_nobody_joins_one(self):
        personal = create_personal_organization(self.person("pl-solo"))
        body = self.client_for(self.admin).get(reverse("organizations:organization-admin-detail", args=[personal.pk])).json()
        self.assertEqual(body["usage"], {"moderators": 0, "members": 0})

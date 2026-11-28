"""The plan's TOTAL seat limit: Free 5, Team 25, Enterprise 100 people, the owner and every invitation still waiting included.

Accounting rules proved here: the owner, moderators and members count once each; a waiting invitation holds a seat until it is accepted, cancelled or expires
and is never counted twice on acceptance; the owner's own seat and personal workspaces are never refused or counted against a shared organization; an
administrator's way in (a moderator account) meets the same limit; a plan cannot be lowered below what an organization uses, and nobody is removed to make room.
"""
import unittest
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from organizations.services import (
    PlanLimitReached, accept_invitation, assert_seat_available, create_organization, create_pending_organization, create_personal_organization,
    hash_token, invite_member, plan_limits, seats_in_use,
)
from organizations.test_owner_invitation_postgres import run_in_parallel

User = get_user_model()
Role = OrganizationMembership.Role
PASSWORD = "A-long-unique-passphrase-7"
LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


class SeatFixtures:
    plan = Organization.Plan.FREE

    def setUp(self):
        cache.clear()
        self.counter = 0
        self.admin = self.person("seat-admin", role="admin")
        self.owner = self.person("seat-owner")
        self.organization = create_organization(name="Seat Co", owner=self.owner, plan=self.plan)

    def person(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def fill(self, count, role=Role.MEMBER, organization=None):
        """``count`` more active people (bulk: a fast way to reach a limit)."""
        organization = organization or self.organization
        base = self.counter
        self.counter += count
        users = User.objects.bulk_create([User(username=f"fill-{base + i}", email=f"fill-{base + i}@example.com", is_email_verified=True) for i in range(count)])
        OrganizationMembership.objects.bulk_create([OrganizationMembership(organization=organization, user=user, role=role) for user in users])

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def invite(self, role=Role.MEMBER, email=None, organization=None, actor=None):
        self.counter += 1
        organization = organization or self.organization
        return self.client_for(actor or self.owner).post(
            reverse("organizations:organization-invitation-list-create", args=[organization.pk]),
            {"email": email or f"invitee-{self.counter}@example.com", "role": role}, format="json")

    def admin_patch(self, body, organization=None):
        organization = organization or self.organization
        return self.client_for(self.admin).patch(reverse("organizations:organization-admin-detail", args=[organization.pk]), body, format="json")


@LOCMEM
class FreePlanTests(SeatFixtures, TestCase):
    def test_the_defaults_are_five_twenty_five_and_one_hundred(self):
        self.assertEqual([plan_limits(Organization(plan=plan))["users"] for plan in ("free", "team", "enterprise")], [5, 25, 100])

    def test_the_owner_counts_and_the_sixth_person_is_refused_with_a_clear_error(self):
        self.assertEqual(seats_in_use(self.organization, "users"), 1)
        for _ in range(4):
            self.assertEqual(self.invite().status_code, 201)
        self.assertEqual(seats_in_use(self.organization, "users"), 5)                        # the owner plus four waiting invitations
        refused = self.invite()
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json(), {"detail": "Your plan allows 5 users.", "code": "plan_limit_reached", "kind": "users", "limit": 5})

    def test_cancelling_a_waiting_invitation_gives_its_seat_back(self):
        for _ in range(3):
            self.invite()
        last = self.invite()
        self.assertEqual(self.invite().status_code, 403)
        OrganizationInvitation.objects.filter(pk=last.json()["id"]).update(status=OrganizationInvitation.Status.CANCELLED)
        self.assertEqual(self.invite().status_code, 201)

    def test_an_expired_invitation_holds_no_seat(self):
        for _ in range(4):
            self.invite()
        OrganizationInvitation.objects.update(expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(seats_in_use(self.organization, "users"), 1)
        self.assertEqual(self.invite().status_code, 201)

    def test_accepting_the_last_waiting_invitation_does_not_count_the_person_twice(self):
        self.fill(3)                                                                         # owner + 3 = 4
        member = self.person("seat-late")
        invitation = OrganizationInvitation.objects.create(
            organization=self.organization, email=member.email, role="member", token_hash=hash_token("late"), invited_by=self.owner,
            expires_at=timezone.now() + timedelta(hours=5))
        self.assertEqual(seats_in_use(self.organization, "users"), 5)                        # the waiting invitation holds the fifth seat
        accept_invitation(token="late", user=member)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "accepted")
        self.assertEqual(seats_in_use(self.organization, "users"), 5)                        # now an active membership, still one seat

    def test_moderators_inviting_members_and_an_administrator_adding_a_moderator_meet_the_same_total(self):
        self.fill(4)
        self.assertEqual(self.invite(role="moderator").status_code, 403)
        with self.assertRaises(PlanLimitReached) as caught:
            assert_seat_available(self.organization, Role.MODERATOR)                          # the path an administrator's moderator account takes
        self.assertEqual(caught.exception.detail["kind"], "users")

    def test_the_owners_own_seat_is_never_refused_and_a_pending_owner_reserves_it(self):
        self.fill(4)
        assert_seat_available(self.organization, Role.OWNER)                                   # does not raise
        waiting, invitation, _ = create_pending_organization(name="Waits", owner_email="boss@example.test", actor=self.admin)
        self.assertEqual((waiting.status, seats_in_use(waiting, "users")), ("pending_owner", 1))

    def test_a_personal_workspace_never_counts_against_a_shared_organization(self):
        member = self.person("seat-both")
        OrganizationMembership.objects.create(organization=self.organization, user=member, role=Role.MEMBER)
        create_personal_organization(member)
        self.assertEqual(seats_in_use(self.organization, "users"), 2)

    def test_a_suspended_or_removed_person_holds_no_seat(self):
        self.fill(2)
        OrganizationMembership.objects.filter(organization=self.organization, role=Role.MEMBER).update(status=OrganizationMembership.Status.SUSPENDED)
        self.assertEqual(seats_in_use(self.organization, "users"), 1)

    def test_an_organization_already_over_its_total_keeps_everyone_and_cannot_add_more(self):
        self.fill(6)                                                                         # 7 people on a plan of 5 (legacy data)
        self.assertEqual(seats_in_use(self.organization, "users"), 7)
        self.assertEqual(self.invite().status_code, 403)
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization).count(), 7)


@LOCMEM
class LargerPlanTests(SeatFixtures, TestCase):
    def test_team_stops_at_twenty_five(self):
        Organization.objects.filter(pk=self.organization.pk).update(plan="team")
        self.organization.refresh_from_db()
        self.fill(23)                                                                        # owner + 23 = 24
        self.assertEqual(self.invite().status_code, 201)                                     # the 25th
        refused = self.invite()
        self.assertEqual((refused.status_code, refused.json()["limit"], refused.json()["kind"]), (403, 25, "users"))

    def test_enterprise_stops_at_one_hundred(self):
        Organization.objects.filter(pk=self.organization.pk).update(plan="enterprise")
        self.organization.refresh_from_db()
        self.fill(98)                                                                        # owner + 98 = 99
        self.assertEqual(self.invite().status_code, 201)                                     # the 100th
        refused = self.invite()
        self.assertEqual((refused.status_code, refused.json()["limit"]), (403, 100))

    def test_an_upgrade_opens_room_at_once(self):
        self.fill(4)
        self.assertEqual(self.invite().status_code, 403)
        self.assertEqual(self.admin_patch({"plan": "team"}).status_code, 200)
        self.assertEqual(self.invite().status_code, 201)


@LOCMEM
class PlanChangeTests(SeatFixtures, TestCase):
    plan = Organization.Plan.TEAM

    def test_a_plan_cannot_be_lowered_below_what_the_organization_uses_and_nobody_is_removed(self):
        self.fill(7)                                                                         # 8 people: fine on Team, too many for Free (5)
        refused = self.admin_patch({"plan": "free"})
        self.assertEqual(refused.status_code, 400, refused.content)
        body = refused.json()
        self.assertEqual((body["code"], body["kind"], body["limit"], body["in_use"]), ("plan_downgrade_over_capacity", "users", 5, 8))
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.plan, "team")
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization).count(), 8)

    def test_waiting_invitations_block_a_downgrade_too_and_cancelling_them_unblocks_it(self):
        self.fill(2)
        for _ in range(3):
            self.invite()                                                                    # 3 active + 3 waiting = 6
        self.assertEqual(self.admin_patch({"plan": "free"}).status_code, 400)
        OrganizationInvitation.objects.update(status=OrganizationInvitation.Status.CANCELLED)
        self.assertEqual(self.admin_patch({"plan": "free"}).status_code, 200)

    def test_a_downgrade_that_fits_and_every_upgrade_are_allowed(self):
        self.fill(2)
        self.assertEqual(self.admin_patch({"plan": "free"}).status_code, 200)
        self.assertEqual(self.admin_patch({"plan": "enterprise"}).status_code, 200)

    def test_other_changes_are_not_blocked_by_an_organization_that_is_over_its_plan(self):
        Organization.objects.filter(pk=self.organization.pk).update(plan="free")
        self.fill(7)
        self.assertEqual(self.admin_patch({"name": "Renamed Co"}).status_code, 200)

    def test_the_numbers_the_screens_show_are_the_numbers_enforced(self):
        self.fill(2)
        self.invite()
        body = self.client_for(self.admin).get(reverse("organizations:organization-admin-detail", args=[self.organization.pk])).json()
        self.assertEqual((body["limits"]["users"], body["usage"]["users"]), (25, 4))
        self.assertEqual(body["usage"]["users"], seats_in_use(self.organization, "users"))


@unittest.skipUnless(connection.vendor == "postgresql", "the seat lock is only exercised on PostgreSQL")
@LOCMEM
class ConcurrentSeatTests(TransactionTestCase):
    """Several invitations for the last seat at the same instant (separate connections): exactly one is created."""

    def test_only_one_of_many_simultaneous_invitations_takes_the_last_seat(self):
        owner = User.objects.create_user(username="cc-owner", email="cc-owner@example.com", password=PASSWORD, is_email_verified=True)
        organization = create_organization(name="Race Co", owner=owner)                      # Free: 5 users
        for index in range(3):
            user = User.objects.create_user(username=f"cc-{index}", email=f"cc-{index}@example.com", password=PASSWORD, is_email_verified=True)
            OrganizationMembership.objects.create(organization=organization, user=user, role=Role.MEMBER)
        membership = OrganizationMembership.objects.get(organization=organization, user=owner)           # 4 seats used, one left
        calls = [lambda n=n: invite_member(organization=organization, actor_membership=membership, email=f"race-{n}@example.com", role="member") for n in range(4)]
        results = run_in_parallel(*calls)
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1, results)
        self.assertTrue(all(isinstance(value, PlanLimitReached) for kind, value in results if kind == "error"), results)
        self.assertEqual(seats_in_use(organization, "users"), 5)

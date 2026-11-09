"""The administrator manages organizations: head counts and owner on the list, rename / plan / suspend / reactivate, and what a suspended organization means."""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from uploads.models import UploadBatch
from uploads.tests import make_image

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from .services import create_organization, create_personal_organization, get_default_organization, hash_token

PASSWORD = "ForestRiver!4821"
Role = OrganizationMembership.Role
State = OrganizationMembership.Status
Action = OrganizationAuditEvent.Action
SUSPENDED_BODY = {"detail": "This organization is suspended.", "code": "organization_suspended"}


class Fixtures:
    def setUp(self):
        cache.clear()
        self.admin = self.person("am-admin", role="admin")
        self.owner = self.person("am-owner")
        self.moderator = self.person("am-mod", role="moderator")
        self.member = self.person("am-member")
        self.outsider = self.person("am-outsider")
        for user in (self.owner, self.moderator, self.member, self.outsider):
            create_personal_organization(user)
        self.organization = create_organization(name="Acme", owner=self.owner, actor=self.admin)
        self.join(self.moderator, Role.MODERATOR)
        self.join(self.member, Role.MEMBER)
        self.url = reverse("organizations:organization-admin-detail", args=[self.organization.pk])
        self.list_url = reverse("organizations:organization-admin-list-create")

    def person(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def join(self, user, role, organization=None, status_value=State.ACTIVE):
        return OrganizationMembership.objects.create(organization=organization or self.organization, user=user, role=role, status=status_value)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def patch(self, body, user=None, url=None):
        return self.client_for(user or self.admin).patch(url or self.url, body, format="json")

    def suspend(self, organization=None):
        Organization.objects.filter(pk=(organization or self.organization).pk).update(status=Organization.Status.SUSPENDED)

    def events(self, action, organization=None):
        return OrganizationAuditEvent.objects.filter(organization=organization or self.organization, action=action)


class ListTests(Fixtures, TestCase):
    def row(self, organization=None):
        rows = self.client_for(self.admin).get(self.list_url).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        return next(row for row in rows if row["id"] == (organization or self.organization).pk)

    def test_each_organization_shows_its_owner_and_its_active_head_counts(self):
        row = self.row()
        self.assertEqual(row["owner"], {"id": self.owner.pk, "username": "am-owner"})
        self.assertEqual((row["member_count"], row["moderator_count"]), (3, 1))             # owner + moderator + member; one moderator
        for field in ("id", "name", "slug", "status", "plan", "is_personal", "moderator_data_scope", "created_at", "updated_at"):
            self.assertIn(field, row)                                                       # nothing that was there is gone

    def test_only_active_memberships_are_counted(self):
        for index, state in enumerate((State.INVITED, State.SUSPENDED, State.REMOVED)):
            self.join(self.person(f"am-other-{index}"), Role.MODERATOR if index == 0 else Role.MEMBER, status_value=state)
        row = self.row()
        self.assertEqual((row["member_count"], row["moderator_count"]), (3, 1))

    def test_a_removed_owner_is_not_the_owner_and_an_organization_without_one_says_null(self):
        OrganizationMembership.objects.filter(organization=self.organization, role=Role.OWNER).update(status=State.REMOVED)
        self.assertIsNone(self.row()["owner"])

    def test_the_oldest_active_owner_is_the_one_shown(self):
        second = self.person("am-second-owner")
        self.join(second, Role.OWNER)
        self.assertEqual(self.row()["owner"]["username"], "am-owner")

    def test_a_page_of_organizations_costs_the_same_number_of_queries_as_one(self):
        client = self.client_for(self.admin)
        client.get(self.list_url)                                                           # warm anything cached
        one = self.count_queries(client)
        for index in range(5):
            create_organization(name=f"More {index}", owner=self.person(f"am-more-{index}"))
        self.assertEqual(self.count_queries(client), one)

    def count_queries(self, client):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            client.get(self.list_url)
        return len(captured)

    def test_creating_an_organization_answers_with_the_same_extra_fields(self):
        response = self.client_for(self.admin).post(self.list_url, {"name": "Fresh", "owner_id": self.outsider.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual(body["owner"], {"id": self.outsider.pk, "username": "am-outsider"})
        self.assertEqual((body["member_count"], body["moderator_count"]), (1, 0))

    def test_only_the_administrator_may_look(self):
        for user in (self.owner, self.moderator, self.member):
            self.assertEqual(self.client_for(user).get(self.list_url).status_code, status.HTTP_403_FORBIDDEN, user.username)
            self.assertEqual(self.client_for(user).get(self.url).status_code, status.HTTP_403_FORBIDDEN, user.username)
        self.assertEqual(APIClient().get(self.list_url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_detail_reads_one_organization(self):
        body = self.client_for(self.admin).get(self.url).json()
        self.assertEqual((body["id"], body["owner"]["username"], body["member_count"]), (self.organization.pk, "am-owner", 3))
        self.assertEqual(self.client_for(self.admin).get(reverse("organizations:organization-admin-detail", args=[999999])).status_code, 404)


class PatchTests(Fixtures, TestCase):
    def test_the_administrator_renames_changes_the_plan_and_suspends_in_one_request(self):
        response = self.patch({"name": "  Acme Ltd  ", "plan": "team", "status": "suspended"})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["name"], body["plan"], body["status"]), ("Acme Ltd", "team", "suspended"))
        self.assertEqual((body["owner"]["username"], body["member_count"]), ("am-owner", 3))
        self.organization.refresh_from_db()
        self.assertEqual((self.organization.name, self.organization.plan, self.organization.status), ("Acme Ltd", "team", "suspended"))

    def test_each_field_can_be_sent_alone(self):
        self.assertEqual(self.patch({"name": "Renamed"}).json()["name"], "Renamed")
        self.assertEqual(self.patch({"plan": "enterprise"}).json()["plan"], "enterprise")
        self.assertEqual(self.patch({"status": "suspended"}).json()["status"], "suspended")
        self.assertEqual(self.patch({"status": "active"}).json()["status"], "active")

    def test_a_bad_value_changes_nothing(self):
        for body in ({"status": "deleted"}, {"plan": "platinum"}, {"name": "   "}, {"name": "x" * 151}, {}, {"reason": "only a reason"}):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 400)
        self.organization.refresh_from_db()
        self.assertEqual((self.organization.name, self.organization.plan, self.organization.status), ("Acme", "free", "active"))
        self.assertFalse(self.events(Action.ORGANIZATION_UPDATED).exists())

    def test_nobody_but_the_administrator_may_change_it_not_even_the_owner(self):
        for user in (self.owner, self.moderator, self.member, self.outsider):
            self.assertEqual(self.patch({"status": "suspended"}, user=user).status_code, 403, user.username)
        self.assertEqual(APIClient().patch(self.url, {"name": "X"}, format="json").status_code, 401)
        self.organization.refresh_from_db()
        self.assertEqual((self.organization.name, self.organization.status), ("Acme", "active"))

    def test_an_unknown_organization_is_404(self):
        self.assertEqual(self.patch({"name": "X"}, url=reverse("organizations:organization-admin-detail", args=[999999])).status_code, 404)

    def test_put_and_delete_are_not_offered(self):
        client = self.client_for(self.admin)
        self.assertEqual(client.put(self.url, {"name": "X"}, format="json").status_code, 405)
        self.assertEqual(client.delete(self.url).status_code, 405)

    def test_a_personal_workspace_cannot_be_suspended_but_can_be_renamed(self):
        personal = Organization.objects.get(is_personal=True, memberships__user=self.member)
        url = reverse("organizations:organization-admin-detail", args=[personal.pk])
        response = self.patch({"status": "suspended"}, url=url)
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.json())
        self.assertEqual(self.patch({"name": "Their space"}, url=url).status_code, 200)
        personal.refresh_from_db()
        self.assertEqual((personal.status, personal.name), ("active", "Their space"))


class AuditTests(Fixtures, TestCase):
    def test_a_rename_and_plan_change_is_one_event_with_the_old_and_new_values(self):
        self.patch({"name": "Acme Ltd", "plan": "team", "reason": "contract signed"})
        event = self.events(Action.ORGANIZATION_UPDATED).get()
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.previous_value, {"name": "Acme", "plan": "free"})
        self.assertEqual(event.new_value, {"name": "Acme Ltd", "plan": "team"})
        self.assertEqual((event.target_type, event.target_id, event.reason), ("Organization", self.organization.pk, "contract signed"))

    def test_suspending_and_reactivating_are_their_own_events(self):
        self.patch({"status": "suspended", "reason": "unpaid invoice"})
        suspended = self.events(Action.ORGANIZATION_SUSPENDED).get()
        self.assertEqual((suspended.previous_value, suspended.new_value, suspended.reason), ({"status": "active"}, {"status": "suspended"}, "unpaid invoice"))
        self.patch({"status": "active"})
        reactivated = self.events(Action.ORGANIZATION_REACTIVATED).get()
        self.assertEqual((reactivated.previous_value, reactivated.new_value, reactivated.actor), ({"status": "suspended"}, {"status": "active"}, self.admin))
        self.assertFalse(self.events(Action.ORGANIZATION_UPDATED).exists())                 # a status change alone is not a "details" event

    def test_a_request_that_changes_nothing_records_nothing(self):
        before = OrganizationAuditEvent.objects.count()
        self.assertEqual(self.patch({"name": "Acme", "plan": "free", "status": "active"}).status_code, 200)
        self.assertEqual(OrganizationAuditEvent.objects.count(), before)

    def test_the_owner_reads_it_in_the_organization_audit_trail_in_words(self):
        self.patch({"status": "suspended"})
        self.patch({"status": "active"})
        self.patch({"plan": "team"})
        rows = self.client_for(self.owner).get(reverse("organizations:organization-audit-list", args=[self.organization.pk])).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        messages = {row["action"]: row["message"] for row in rows}
        self.assertEqual(messages["organization_suspended"], "Suspended the organization.")
        self.assertEqual(messages["organization_reactivated"], "Reactivated the organization.")
        self.assertEqual(messages["organization_updated"], "Changed the organization's details.")


class SuspendedOrganizationTests(Fixtures, TestCase):
    def organization_urls(self):
        pk = self.organization.pk
        return [
            reverse("organizations:organization-detail", args=[pk]),
            reverse("organizations:organization-member-list", args=[pk]),
            reverse("organizations:organization-invitation-list-create", args=[pk]),
            reverse("organizations:organization-audit-list", args=[pk]),
            f"/api/organizations/{pk}/moderator/dashboard/",
            f"/api/organizations/{pk}/moderator/reviews/",
            f"/api/organizations/{pk}/moderator/batches/",
            f"/api/organizations/{pk}/export/",
        ]

    def test_before_suspension_the_same_endpoints_answer_for_the_people_in_it(self):
        for url in self.organization_urls():
            self.assertEqual(self.client_for(self.owner).get(url).status_code, 200, url)

    def test_members_moderators_and_the_owner_get_403_with_one_sentence_on_organization_endpoints(self):
        self.patch({"status": "suspended"})
        for user in (self.owner, self.moderator, self.member):
            for url in self.organization_urls():
                response = self.client_for(user).get(url)
                self.assertEqual(response.status_code, 403, (user.username, url))
                self.assertEqual(response.json(), SUSPENDED_BODY, (user.username, url))

    def test_writing_is_refused_too(self):
        self.patch({"status": "suspended"})
        pk = self.organization.pk
        invite = self.client_for(self.owner).post(
            reverse("organizations:organization-invitation-list-create", args=[pk]), {"email": "new@example.com", "role": "member"}, format="json")
        self.assertEqual((invite.status_code, invite.json()), (403, SUSPENDED_BODY))
        membership = OrganizationMembership.objects.get(user=self.member, organization=self.organization)
        removal = self.client_for(self.owner).delete(reverse("organizations:organization-member-detail", args=[pk, membership.pk]))
        self.assertEqual(removal.status_code, 403)
        membership.refresh_from_db()
        self.assertEqual(membership.status, State.ACTIVE)
        self.assertFalse(OrganizationInvitation.objects.exists())

    def test_someone_outside_still_gets_404_so_the_suspension_is_not_revealed(self):
        self.patch({"status": "suspended"})
        self.assertEqual(self.client_for(self.outsider).get(reverse("organizations:organization-detail", args=[self.organization.pk])).status_code, 404)

    def test_the_list_still_shows_the_organization_as_suspended_with_nothing_offered(self):
        self.patch({"status": "suspended"})
        rows = self.client_for(self.member).get(reverse("organizations:organization-list")).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        row = next(row for row in rows if row["id"] == self.organization.pk)
        self.assertEqual(row["status"], "suspended")
        self.assertEqual(row["membership"]["role"], "member")
        self.assertFalse(any(row["capabilities"].values()))
        personal = next(row for row in rows if row["is_personal"])
        self.assertEqual(personal["status"], "active")

    def test_uploads_to_it_are_refused_and_nothing_is_stored(self):
        self.patch({"status": "suspended"})
        response = self.client_for(self.member).post(
            reverse("image-upload-single"), {"file": make_image(), "organization_id": self.organization.pk}, format="multipart")
        self.assertEqual((response.status_code, response.json()), (403, SUSPENDED_BODY))
        self.assertFalse(UploadBatch.objects.exists())

    def test_the_personal_workspace_keeps_working(self):
        self.patch({"status": "suspended"})
        response = self.client_for(self.member).post(reverse("image-upload-single"), {"file": make_image()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(UploadBatch.objects.get().organization.is_personal)
        self.assertEqual(self.client_for(self.member).get(reverse("organizations:organization-list")).status_code, 200)

    def test_a_suspended_organization_is_never_the_default_for_someone_without_a_personal_one(self):
        lone = self.person("am-lone")
        self.join(lone, Role.MEMBER)
        self.assertEqual(get_default_organization(lone), self.organization)
        self.suspend()
        self.assertIsNone(get_default_organization(lone))

    def test_reactivating_restores_everything(self):
        pk = self.organization.pk
        self.patch({"status": "suspended"})
        self.assertEqual(self.client_for(self.owner).get(reverse("organizations:organization-detail", args=[pk])).status_code, 403)
        self.patch({"status": "active"})
        for url in self.organization_urls():
            self.assertEqual(self.client_for(self.owner).get(url).status_code, 200, url)
        self.assertEqual(self.client_for(self.member).post(
            reverse("image-upload-single"), {"file": make_image(), "organization_id": pk}, format="multipart").status_code, 201)
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization, status=State.ACTIVE).count(), 3)

    def test_an_invitation_to_a_suspended_organization_cannot_be_accepted_or_previewed_until_it_is_back(self):
        invited = self.person("am-invited")
        token = "invite-token-for-suspension-test-0123456789"
        OrganizationInvitation.objects.create(
            organization=self.organization, email=invited.email, role=Role.MEMBER, token_hash=hash_token(token),
            invited_by=self.owner, expires_at=timezone.now() + timedelta(hours=24),
        )
        accept = lambda: self.client_for(invited).post(reverse("organizations:organization-invitation-accept"), {"token": token}, format="json")  # noqa: E731
        preview = lambda: APIClient().get(reverse("organizations:organization-invitation-preview", args=[token]))                                    # noqa: E731
        self.assertEqual(preview().status_code, 200)
        self.patch({"status": "suspended"})
        self.assertEqual(preview().status_code, 404)
        refused = accept()
        self.assertEqual((refused.status_code, refused.json()), (403, SUSPENDED_BODY))
        self.assertFalse(OrganizationMembership.objects.filter(user=invited).exclude(organization__is_personal=True).exists())
        self.patch({"status": "active"})
        self.assertEqual(preview().status_code, 200)
        self.assertEqual(accept().status_code, 200)

    @override_settings(THROTTLE_RATE_LOGIN="1000/hour")
    def test_a_moderator_whose_only_organization_is_suspended_is_told_why_at_sign_in(self):
        cache.clear()
        sign_in = lambda: APIClient().post(reverse("login"), {"identifier": self.moderator.username, "password": PASSWORD}, format="json")  # noqa: E731
        self.assertEqual(sign_in().status_code, 200)
        self.patch({"status": "suspended"})
        response = sign_in()
        self.assertEqual((response.status_code, response.json()), (403, SUSPENDED_BODY))
        self.patch({"status": "active"})
        self.assertEqual(sign_in().status_code, 200)

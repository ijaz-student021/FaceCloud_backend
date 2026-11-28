"""Why a moderator an administrator created 'Active' could not sign in, and what the administrator now sees.

Reproduced here: an account created WITHOUT a password has an unusable password, so the sign-in answers the generic "Invalid username/email or password." (and the same
answer comes for an address that exists on another server). The administrator's list now says ``password_setup_pending`` / ``login_ready: false`` and shows whether the
setup e-mail was accepted by the mail server; the resend action is honest when the mail server refuses.
"""
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership
from organizations.services import create_organization, seats_in_use

User = get_user_model()
PASSWORD = "A-long-unique-passphrase-7"
REASON = "Needs a moderator for the pilot event"
GENERIC = "Invalid username/email or password."


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="http://localhost:5174")
class ModeratorOnboardingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="mo-admin", email="mo-admin@example.com", password=PASSWORD, role="admin", is_email_verified=True)
        self.owner = User.objects.create_user(username="mo-owner", email="mo-owner@example.com", password=PASSWORD, is_email_verified=True)
        self.organization = create_organization(name="Pilot Co", owner=self.owner, plan="team")
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(self.admin)

    def create_moderator(self, **extra):
        body = {"username": "mo-mod", "email": "mo-mod@example.com", "role": "moderator", "organization_id": self.organization.pk, "reason": REASON, **extra}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client_admin.post(reverse("admin-user-list"), body, format="json")

    def sign_in(self, identifier="mo-mod@example.com", password=PASSWORD):
        return APIClient().post(reverse("login"), {"identifier": identifier, "password": password}, format="json")

    def row(self, username="mo-mod"):
        rows = self.client_admin.get(reverse("admin-user-list")).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        return next(item for item in rows if item["username"] == username)

    def setup_link(self):
        match = re.search(r"/reset-password/([^/\s]+)/([^/\s]+)", mail.outbox[-1].body)
        return match.group(1), match.group(2)

    # ---- the reproduction
    def test_a_moderator_created_without_a_password_is_active_but_cannot_sign_in_and_the_list_now_says_so(self):
        self.assertEqual(self.create_moderator().status_code, 201)
        refused = self.sign_in()
        self.assertEqual((refused.status_code, refused.json()["detail"]), (400, [GENERIC]))             # the exact symptom: nothing says why
        row = self.row()
        self.assertTrue(row["is_active"] and row["is_email_verified"])                                  # the old 'Active' badge
        self.assertEqual((row["password_setup_pending"], row["login_ready"], row["login_blockers"]), (True, False, ["password_setup_pending"]))
        self.assertEqual(row["setup_email"]["status"], "sent")                                          # accepted by the (test) mail server, never 'delivered'
        self.assertEqual(row["setup_email"]["kind"], "account_created")
        self.assertEqual([(m["status"], m["organization_status"]) for m in row["moderator_memberships"]], [("active", "active")])

    def test_the_setup_link_lets_the_person_choose_a_password_and_then_sign_in_to_their_organization(self):
        self.create_moderator()
        uid, token = self.setup_link()
        done = APIClient().post(reverse("password-reset-confirm"), {"uidb64": uid, "token": token, "new_password": PASSWORD}, format="json")
        self.assertEqual(done.status_code, 200, done.content)
        signed_in = self.sign_in()
        self.assertEqual(signed_in.status_code, 200, signed_in.content)
        self.assertEqual(self.row()["login_ready"], True)
        self.assertEqual(self.row()["login_blockers"], [])
        self.assertEqual(User.objects.get(username="mo-mod").role, "moderator")

    def test_a_moderator_created_with_a_password_is_ready_at_once(self):
        self.assertEqual(self.create_moderator(password=PASSWORD).status_code, 201)
        row = self.row()
        self.assertEqual((row["password_setup_pending"], row["login_ready"]), (False, True))
        self.assertEqual(self.sign_in().status_code, 200)

    def test_no_response_ever_carries_a_link_or_a_token(self):
        self.create_moderator()
        listing = self.client_admin.get(reverse("admin-user-list")).content.decode()
        self.assertNotIn("reset-password", listing)
        self.assertNotIn(self.setup_link()[1], listing)

    # ---- the other reasons a moderator 'cannot sign in'
    def test_a_suspended_moderator_membership_is_a_blocker_and_sign_in_is_refused(self):
        self.create_moderator(password=PASSWORD)
        OrganizationMembership.objects.filter(user__username="mo-mod", organization=self.organization).update(status="suspended")
        self.assertEqual(self.row()["login_blockers"], ["no_active_moderator_membership"])
        refused = self.sign_in()
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "no_organization")

    def test_a_suspended_organization_is_a_blocker_too(self):
        self.create_moderator(password=PASSWORD)
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        self.assertIn("no_active_moderator_membership", self.row()["login_blockers"])
        self.assertEqual(self.sign_in().json()["code"], "organization_suspended")

    def test_a_full_organization_never_stops_an_existing_moderator_signing_in(self):
        Organization.objects.filter(pk=self.organization.pk).update(plan="free")
        self.organization.refresh_from_db()
        self.create_moderator(password=PASSWORD)
        for index in range(3):
            user = User.objects.create_user(username=f"mo-fill-{index}", email=f"mo-fill-{index}@example.com", password=PASSWORD, is_email_verified=True)
            OrganizationMembership.objects.create(organization=self.organization, user=user, role="member")
        self.assertEqual(seats_in_use(self.organization, "users"), 5)                                    # Free: owner + moderator + 3 members = full
        self.assertEqual(self.sign_in().status_code, 200)

    def test_an_unknown_address_gets_the_same_answer_as_a_wrong_password(self):
        self.create_moderator(password=PASSWORD)
        unknown = self.sign_in("nobody-here@example.com")
        wrong = self.sign_in(password="a-wrong-password-9")
        self.assertEqual((unknown.status_code, unknown.json()), (wrong.status_code, wrong.json()))        # no account enumeration

    def test_an_unverified_account_still_cannot_sign_in(self):
        self.create_moderator(password=PASSWORD)
        User.objects.filter(username="mo-mod").update(is_email_verified=False)
        self.assertIn("email_unverified", self.row()["login_blockers"])
        self.assertEqual(self.sign_in().json()["detail"], ["Email verification is required."])

    def test_an_invited_moderator_is_listed_as_a_moderator_and_says_when_the_portal_will_refuse(self):
        person = User.objects.create_user(username="mo-invited", email="mo-invited@example.com", password=PASSWORD, is_email_verified=True)      # role stays "user"
        membership = OrganizationMembership.objects.create(organization=self.organization, user=person, role="moderator")
        rows = self.client_admin.get(reverse("admin-user-list") + "?role=moderator").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertIn("mo-invited", [row["username"] for row in rows])
        self.assertNotIn("mo-owner", [row["username"] for row in rows])
        self.assertEqual((self.row("mo-invited")["moderator_portal_ready"], self.row("mo-owner")["moderator_portal_ready"]), (True, None))
        membership.status = "suspended"
        membership.save()
        row = self.row("mo-invited")
        self.assertEqual((row["login_ready"], row["moderator_portal_ready"]), (True, False))          # can sign in as a user, the portal refuses
        refused = self.sign_in("mo-invited@example.com")
        self.assertEqual(refused.status_code, 200)

    # ---- resend, honestly
    def test_resend_sends_a_new_link_and_the_list_shows_it(self):
        self.create_moderator()
        before = len(mail.outbox)
        url = reverse("admin-user-password-reset", args=[User.objects.get(username="mo-mod").pk])
        response = self.client_admin.post(url)
        self.assertEqual((response.status_code, response.json()["email_status"]), (200, "sent"))
        self.assertEqual(len(mail.outbox), before + 1)
        self.assertNotIn("reset-password", response.content.decode())
        self.assertEqual(self.row()["setup_email"]["kind"], "password_reset")

    def test_a_mail_server_that_refuses_is_reported_not_hidden(self):
        self.create_moderator()
        url = reverse("admin-user-password-reset", args=[User.objects.get(username="mo-mod").pk])
        with mock.patch("accounts.services.send_mail", side_effect=OSError("SMTP unreachable")):
            response = self.client_admin.post(url)
        self.assertEqual((response.status_code, response.json()["code"]), (502, "email_failed"))
        self.assertEqual(self.row()["setup_email"]["status"], "failed")

    def test_a_failed_first_e_mail_is_visible_on_the_list(self):
        with mock.patch("accounts.moderator_emails.send_mail", side_effect=OSError("SMTP unreachable")):
            self.assertEqual(self.create_moderator().status_code, 201)                                    # the account is still created
        self.assertEqual(self.row()["setup_email"]["status"], "failed")

    def test_only_an_administrator_can_resend(self):
        self.create_moderator()
        url = reverse("admin-user-password-reset", args=[User.objects.get(username="mo-mod").pk])
        other = APIClient()
        other.force_authenticate(self.owner)
        self.assertEqual(other.post(url).status_code, 403)


class ListCostTests(TestCase):
    def test_the_list_costs_the_same_number_of_queries_for_two_people_or_thirty(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        admin = User.objects.create_user(username="lc-admin", email="lc-admin@example.com", password=PASSWORD, role="admin", is_email_verified=True)
        client = APIClient()
        client.force_authenticate(admin)

        def cost():
            with CaptureQueriesContext(connection) as captured:
                client.get(reverse("admin-user-list"))
            return len(captured)

        owner = User.objects.create_user(username="lc-owner", email="lc-owner@example.com", password=PASSWORD, is_email_verified=True)
        organization = create_organization(name="Cost Co", owner=owner, plan="enterprise")
        for index in range(2):
            moderator = User.objects.create_user(username=f"lc-m{index}", email=f"lc-m{index}@example.com", role="moderator", is_email_verified=True)
            OrganizationMembership.objects.create(organization=organization, user=moderator, role="moderator")
        few = cost()
        for index in range(28):
            moderator = User.objects.create_user(username=f"lc-x{index}", email=f"lc-x{index}@example.com", role="moderator", is_email_verified=True)
            OrganizationMembership.objects.create(organization=organization, user=moderator, role="moderator")
        self.assertEqual(cost(), few)

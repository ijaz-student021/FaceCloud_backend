"""The e-mails about an account's role: an accepted invitation, an administrator's new account, a promotion to moderator. None of them ever carries a password."""
import re
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import OrganizationMembership
from organizations.services import create_organization, update_member

from .models import User

PASSWORD = "ForestRiver!4821"
CHOSEN = "BrandNew!Passphrase-7731"
SITE = "https://app.facecloud.test"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL=SITE)
class ModeratorEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = self.make("mail-admin", role="admin")
        self.owner = self.make("mail-owner")
        self.organization = create_organization(name="Mail Co", owner=self.owner)

    def make(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def sent_to(self, address):
        return [message for message in mail.outbox if address in message.to]

    # -- the invitation: link first, then "your account is ready" after it was accepted -----------------------------------------------------------------------
    def test_an_invited_moderator_gets_the_link_and_after_accepting_an_account_is_ready_mail_without_the_password(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.admin).post("/api/user/admin/invitations/", {"email": "newbie@example.org", "role": "moderator", "reason": "Pilot organization needs a moderator before its owner can invite one", "organization_id": self.organization.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        invitation_mail = self.sent_to("newbie@example.org")[-1]
        self.assertIn(f"{SITE}/accept-invitation?token=", invitation_mail.body)                        # step 1: the link
        token = re.search(r"token=([\w-]+)", invitation_mail.body).group(1)

        before = len(mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            accepted = APIClient().post("/api/auth/invitations/accept/", {"token": token, "username": "newbie", "password": CHOSEN}, format="json")
        self.assertEqual(accepted.status_code, 201, accepted.content)
        ready = mail.outbox[before:]
        self.assertEqual(len(ready), 1)                                                                # step 2: exactly one "ready" mail
        body = ready[0].body
        self.assertEqual(ready[0].to, ["newbie@example.org"])
        self.assertIn("Email: newbie@example.org", body)
        self.assertIn("Username: newbie", body)
        self.assertIn("Role: moderator", body)
        self.assertIn("Organization: Mail Co", body)
        self.assertIn(f"{SITE}/login", body)
        self.assertNotIn(CHOSEN, body)                                                                 # the password is never e-mailed
        self.assertNotIn(CHOSEN, ready[0].subject)

    # -- an administrator creates the account ---------------------------------------------------------------------------------------------------------------
    def test_an_account_created_by_an_administrator_gets_sign_in_details_and_a_choose_password_link_but_never_the_password(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.admin).post(
                "/api/user/admin/users/", {"username": "sara", "email": "sara@example.org", "password": CHOSEN, "role": "moderator", "reason": "Pilot organization needs a moderator before its owner can invite one", "organization_id": self.organization.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        message = self.sent_to("sara@example.org")[0]
        self.assertIn("Email: sara@example.org", message.body)
        self.assertIn("Organization: Mail Co", message.body)
        self.assertRegex(message.body, rf"{re.escape(SITE)}/reset-password/[\w-]+/[\w-]+")           # the way to choose a password
        self.assertNotIn(CHOSEN, message.body)
        self.assertNotIn(CHOSEN, message.subject)

    # -- an existing account becomes a moderator -------------------------------------------------------------------------------------------------------------
    def test_promoting_an_account_to_moderator_sends_one_mail_with_the_panel_link(self):
        person = self.make("promoted")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.admin).patch(f"/api/user/admin/users/{person.pk}/", {"role": "moderator", "reason": "Pilot organization needs a moderator before its owner can invite one", "organization_id": self.organization.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        messages = self.sent_to("promoted@example.com")
        self.assertEqual(len(messages), 1)
        self.assertIn("You are now a moderator for Mail Co", messages[0].body)
        self.assertIn(f"{SITE}/moderator", messages[0].body)

    def test_changing_something_else_about_an_account_sends_no_mail(self):
        person = self.make("quiet")
        with self.captureOnCommitCallbacks(execute=True):
            self.client_for(self.admin).patch(f"/api/user/admin/users/{person.pk}/", {"is_suspended": False}, format="json")
        self.assertEqual(self.sent_to("quiet@example.com"), [])

    def test_an_owner_making_a_member_a_moderator_sends_the_mail_once(self):
        person = self.make("member-to-mod")
        membership = OrganizationMembership.objects.create(organization=self.organization, user=person, role=OrganizationMembership.Role.MEMBER, status=OrganizationMembership.Status.ACTIVE)
        owner_membership = OrganizationMembership.objects.get(organization=self.organization, user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            update_member(membership=membership, actor_membership=owner_membership, role=OrganizationMembership.Role.MODERATOR)
        self.assertEqual(len(self.sent_to("member-to-mod@example.com")), 1)
        with self.captureOnCommitCallbacks(execute=True):
            update_member(membership=membership, actor_membership=owner_membership, role=OrganizationMembership.Role.MODERATOR)       # unchanged: no second mail
        self.assertEqual(len(self.sent_to("member-to-mod@example.com")), 1)

    # -- a mail problem never undoes the action --------------------------------------------------------------------------------------------------------------
    def test_a_mail_server_failure_does_not_fail_the_administrators_action(self):
        person = self.make("unlucky")
        with mock.patch("accounts.moderator_emails.send_mail", side_effect=OSError("smtp down")):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client_for(self.admin).patch(f"/api/user/admin/users/{person.pk}/", {"role": "moderator", "reason": "Pilot organization needs a moderator before its owner can invite one", "organization_id": self.organization.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        person.refresh_from_db()
        self.assertEqual(person.role, "moderator")
        self.assertTrue(OrganizationMembership.objects.filter(user=person, role="moderator", status="active").exists())

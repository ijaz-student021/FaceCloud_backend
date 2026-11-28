"""A moderator or member invitation whose e-mail cannot be sent: the request still succeeds, the invitation says so, and a resend can recover it."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from organizations.models import OrganizationInvitation
from organizations.services import create_organization

User = get_user_model()
PASSWORD = "A-long-unique-passphrase-7"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="http://localhost:5174")
class InvitationEmailOutcomeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="ie-owner", email="ie-owner@example.com", password=PASSWORD, is_email_verified=True)
        self.organization = create_organization(name="Mail Co", owner=self.owner, plan="team")
        self.client_owner = APIClient()
        self.client_owner.force_authenticate(self.owner)
        self.list_url = reverse("organizations:organization-invitation-list-create", args=[self.organization.pk])

    def invite(self, role="moderator", email="invited@example.com"):
        return self.client_owner.post(self.list_url, {"email": email, "role": role}, format="json")

    def test_a_delivered_invitation_says_sent(self):
        created = self.invite()
        self.assertEqual((created.status_code, created.json()["email_status"], created.json()["email_failure_reason"]), (201, "sent", ""))
        self.assertEqual(len(mail.outbox), 1)

    def test_a_mail_server_that_refuses_gives_a_201_with_failed_not_a_500_with_a_hidden_invitation(self):
        with mock.patch("organizations.services.send_mail", side_effect=OSError("SMTP unreachable")):
            created = self.invite()
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["email_status"], "failed")
        self.assertIn("OSError", created.json()["email_failure_reason"])
        self.assertEqual(OrganizationInvitation.objects.get().status, "pending")
        listed = self.client_owner.get(self.list_url).json()
        listed = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual([row["email_status"] for row in listed], ["failed"])

    def test_the_failure_reason_never_holds_the_link_or_the_token(self):
        with mock.patch("organizations.services.send_mail", side_effect=lambda **kwargs: (_ for _ in ()).throw(OSError(kwargs["message"]))):
            created = self.invite()
        self.assertEqual(created.json()["email_status"], "failed")
        self.assertNotIn("/invitations/", created.content.decode())

    def test_a_resend_recovers_a_failed_invitation(self):
        with mock.patch("organizations.services.send_mail", side_effect=OSError("down")):
            created = self.invite(role="member").json()
        resend = reverse("organizations:organization-invitation-resend", args=[self.organization.pk, created["id"]])
        again = self.client_owner.post(resend)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["email_status"], "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/invitations/", mail.outbox[0].body)

    def test_the_invitation_still_holds_its_seat_while_it_failed_and_can_be_cancelled(self):
        with mock.patch("organizations.services.send_mail", side_effect=OSError("down")):
            created = self.invite().json()
        cancel = reverse("organizations:organization-invitation-cancel", args=[self.organization.pk, created["id"]])
        self.assertEqual(self.client_owner.delete(cancel).status_code, 200)

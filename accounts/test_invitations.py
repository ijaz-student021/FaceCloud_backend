"""Administrator account invitations (verified email) and role-granting rules."""
import re

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization
from organizations.services import create_organization

from .models import AccountInvitation, AuthEvent, User

PASSWORD = "ForestRiver!4821"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   FRONTEND_URL="https://app.facecloud.test")
class AccountInvitationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = self.make_user("inv-admin", role="admin")
        self.superuser = self.make_user("inv-root", role="admin", is_superuser=True)
        self.organization = create_organization(name="Invite Co", owner=self.make_user("inv-owner"))

    def make_user(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD,
                                        is_email_verified=True, **extra)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def invite(self, actor, email="new.person@example.org", role="moderator", **extra):
        body = {"email": email, "role": role, **extra}
        if role == "moderator" and "organization_id" not in body:
            body["organization_id"] = self.organization.pk                   # a moderator invitation names its organization
        with self.captureOnCommitCallbacks(execute=True):
            return self.client_for(actor).post("/api/user/admin/invitations/", body, format="json")

    def token_from_mail(self):
        return re.search(r"token=([\w-]+)", mail.outbox[-1].body).group(1)

    def test_an_invitation_creates_a_verified_account_with_the_granted_role(self):
        response = self.invite(self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(mail.outbox[-1].to, ["new.person@example.org"])
        token = self.token_from_mail()
        self.assertNotIn(token, str(AccountInvitation.objects.values()))  # only its hash is stored

        preview = APIClient().post("/api/auth/invitations/inspect/", {"token": token}, format="json").json()
        self.assertEqual((preview["email"], preview["role"]), ("new.person@example.org", "moderator"))
        accepted = APIClient().post("/api/auth/invitations/accept/",
                                    {"token": token, "username": "newperson", "password": PASSWORD}, format="json")
        self.assertEqual(accepted.status_code, 201, accepted.content)
        self.assertIn("access", accepted.json())
        user = User.objects.get(username="newperson")
        self.assertEqual((user.email, user.role, user.is_email_verified), ("new.person@example.org", "moderator", True))
        self.assertTrue(Organization.objects.filter(memberships__user=user, is_personal=True).exists())
        self.assertEqual(AccountInvitation.objects.get().status, "accepted")
        self.assertTrue(AuthEvent.objects.filter(event="account_invited").exists())
        self.assertTrue(AuthEvent.objects.filter(event="invitation_accepted", user=user).exists())

    def test_a_token_works_once(self):
        self.invite(self.admin)
        token = self.token_from_mail()
        body = {"token": token, "username": "first", "password": PASSWORD}
        self.assertEqual(APIClient().post("/api/auth/invitations/accept/", body, format="json").status_code, 201)
        body["username"] = "second"
        self.assertEqual(APIClient().post("/api/auth/invitations/accept/", body, format="json").status_code, 400)

    def test_an_expired_or_cancelled_invitation_cannot_be_used(self):
        self.invite(self.admin)
        token = self.token_from_mail()
        AccountInvitation.objects.update(expires_at=timezone.now())
        self.assertEqual(APIClient().post("/api/auth/invitations/inspect/", {"token": token}, format="json").status_code, 404)
        self.invite(self.admin, email="other@example.org")
        other = AccountInvitation.objects.get(email="other@example.org")
        self.assertEqual(self.client_for(self.admin).delete(f"/api/user/admin/invitations/{other.pk}/").status_code, 204)
        response = APIClient().post("/api/auth/invitations/accept/",
                                    {"token": self.token_from_mail(), "username": "x1", "password": PASSWORD},
                                    format="json")
        self.assertEqual(response.status_code, 400)

    def test_only_a_superuser_may_grant_admin(self):
        self.assertEqual(self.invite(self.admin, role="admin").status_code, 403)
        self.assertEqual(self.invite(self.superuser, role="admin").status_code, 201)
        target = self.make_user("target")
        patch = self.client_for(self.admin).patch(f"/api/user/admin/users/{target.pk}/", {"role": "admin"}, format="json")
        self.assertEqual(patch.status_code, 403)
        self.assertEqual(self.client_for(self.admin).patch(f"/api/user/admin/users/{target.pk}/",
                                                           {"role": "moderator", "organization_id": self.organization.pk},
                                                           format="json").status_code, 200)

    def test_non_administrators_cannot_invite(self):
        moderator = self.make_user("inv-mod", role="moderator")
        self.assertEqual(self.invite(moderator).status_code, 403)

    def test_duplicates_are_conflicts(self):
        self.assertEqual(self.invite(self.admin, email="inv-admin@example.com").status_code, 409)  # has an account
        self.assertEqual(self.invite(self.admin).status_code, 201)
        self.assertEqual(self.invite(self.admin, email="NEW.PERSON@example.org").status_code, 409)  # pending

    def test_resend_issues_a_new_token_and_the_old_one_stops_working(self):
        self.invite(self.admin)
        old = self.token_from_mail()
        invitation = AccountInvitation.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client_for(self.admin).post(
                f"/api/user/admin/invitations/{invitation.pk}/resend/").status_code, 200)
        new = self.token_from_mail()
        self.assertNotEqual(old, new)
        self.assertEqual(APIClient().post("/api/auth/invitations/inspect/", {"token": old}, format="json").status_code, 404)
        self.assertEqual(APIClient().post("/api/auth/invitations/inspect/", {"token": new}, format="json").status_code, 200)

    def test_weak_passwords_and_taken_usernames_are_refused(self):
        self.invite(self.admin)
        token = self.token_from_mail()
        weak = APIClient().post("/api/auth/invitations/accept/", {"token": token, "username": "abc", "password": "123"},
                                format="json")
        self.assertEqual(weak.status_code, 400)
        taken = APIClient().post("/api/auth/invitations/accept/",
                                 {"token": token, "username": "inv-admin", "password": PASSWORD}, format="json")
        self.assertEqual(taken.status_code, 400)
        self.assertEqual(AccountInvitation.objects.get().status, "pending")

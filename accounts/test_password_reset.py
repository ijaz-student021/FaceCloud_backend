"""The forgot-password flow: request an email, follow its link, set a new password."""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="https://app.example.test")
class ForgotPasswordTests(APITestCase):
    old = "StrongPass!4729"
    new = "ResetStrong!5931"

    def setUp(self):
        self.user = User.objects.create_user(username="alice", email="alice@example.com", password=self.old, is_email_verified=True)

    def request_reset(self, email="alice@example.com"):
        return self.client.post(reverse("password-reset"), {"email": email}, format="json")

    def link_parts(self):
        body = mail.outbox[-1].body
        match = re.search(r"https://app\.example\.test/reset-password/([^/\s]+)/([^/\s]+)", body)
        self.assertIsNotNone(match, body)
        return match.group(1), match.group(2)

    def confirm(self, uid, token, password=None):
        return self.client.post(reverse("password-reset-confirm"), {"uidb64": uid, "token": token, "new_password": password or self.new},
                                format="json")

    def test_the_email_carries_a_working_frontend_link_and_the_password_changes(self):
        self.assertEqual(self.request_reset().status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.com"])
        uid, token = self.link_parts()
        self.assertEqual(self.confirm(uid, token).status_code, status.HTTP_200_OK)
        login = lambda password: self.client.post(reverse("login"), {"email": "alice@example.com", "password": password}, format="json")  # noqa: E731
        self.assertEqual(login(self.new).status_code, status.HTTP_200_OK)
        self.assertEqual(login(self.old).status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_link_works_once(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.assertEqual(self.confirm(uid, token).status_code, status.HTTP_200_OK)
        self.assertEqual(self.confirm(uid, token, "AnotherStrong!8392").status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_tampered_or_foreign_token_is_refused(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.assertEqual(self.confirm(uid, token[:-2] + "xx").status_code, status.HTTP_400_BAD_REQUEST)
        other = User.objects.create_user(username="bob", email="bob@example.com", password=self.old, is_email_verified=True)
        self.client.post(reverse("password-reset"), {"email": "bob@example.com"}, format="json")
        bob_uid, _bob_token = self.link_parts()
        self.assertEqual(self.confirm(bob_uid, token).status_code, status.HTTP_400_BAD_REQUEST)   # alice's token on bob's id
        other.refresh_from_db()
        self.assertTrue(other.check_password(self.old))

    def test_a_weak_new_password_is_refused_and_the_old_one_stays(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.assertEqual(self.confirm(uid, token, "12345678").status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old))

    def test_the_response_does_not_reveal_whether_an_account_exists(self):
        known, unknown = self.request_reset(), self.request_reset("nobody@example.com")
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(len(mail.outbox), 1)

    def test_suspended_and_inactive_accounts_get_no_email(self):
        User.objects.filter(pk=self.user.pk).update(is_suspended=True)
        self.request_reset()
        User.objects.filter(pk=self.user.pk).update(is_suspended=False, is_active=False)
        self.request_reset()
        self.assertEqual(len(mail.outbox), 0)

    def test_resetting_signs_out_existing_sessions(self):
        login = self.client.post(reverse("login"), {"email": "alice@example.com", "password": self.old}, format="json")
        access = login.json()["access"]
        self.request_reset()
        uid, token = self.link_parts()
        self.confirm(uid, token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(self.client.get(reverse("profile")).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_request_is_rate_limited(self):
        from django.core.cache import cache

        cache.clear()
        codes = [self.request_reset().status_code for _ in range(12)]
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, codes)

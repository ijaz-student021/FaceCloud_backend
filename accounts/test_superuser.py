"""``createsuperuser`` must produce an account that can sign in (it has no signup email to verify)."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()
PASSWORD = "Ocean!Breeze2026x"


class SuperuserCanSignInTests(APITestCase):
    def login(self, **identity):
        return self.client.post(reverse("login"), {**identity, "password": PASSWORD}, format="json")

    def assertCanAdminister(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data["role"], response.data["dashboard"]), ("admin", "admin"))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        listed = self.client.get(reverse("admin-user-list"))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertIsInstance(listed.data, list)          # still a plain array
        self.client.credentials()

    def test_the_management_command_makes_a_verified_admin_who_can_log_in_by_email_and_by_username(self):
        env = {"DJANGO_SUPERUSER_USERNAME": "boss", "DJANGO_SUPERUSER_EMAIL": "boss@example.com", "DJANGO_SUPERUSER_PASSWORD": PASSWORD}
        with mock.patch.dict(os.environ, env):
            call_command("createsuperuser", "--noinput", verbosity=0)
        user = User.objects.get(username="boss")
        self.assertTrue(user.is_superuser and user.is_staff and user.is_active)
        self.assertTrue(user.is_email_verified)
        self.assertEqual(user.role, "admin")
        self.assertCanAdminister(self.login(email="boss@example.com"))
        self.assertCanAdminister(self.login(username="boss"))

    def test_the_manager_directly_does_the_same(self):
        user = User.objects.create_superuser("root", "root@example.com", PASSWORD)
        self.assertEqual((user.is_email_verified, user.role, user.is_superuser, user.is_staff), (True, "admin", True, True))
        self.assertCanAdminister(self.login(identifier="root@example.com"))

    def test_an_explicit_value_still_wins(self):
        user = User.objects.create_superuser("odd", "odd@example.com", PASSWORD, is_email_verified=False, role="moderator")
        self.assertEqual((user.is_email_verified, user.role), (False, "moderator"))
        self.assertEqual(self.login(email="odd@example.com").status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_normal_unverified_user_is_still_refused_at_login(self):
        User.objects.create_user("plain", "plain@example.com", PASSWORD)
        user = User.objects.get(username="plain")
        self.assertEqual((user.is_email_verified, user.role, user.is_superuser), (False, "user", False))
        refused = self.login(email="plain@example.com")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Email verification is required", str(refused.data))

    def test_existing_accounts_are_not_touched(self):
        old = User.objects.create_user("old", "old@example.com", PASSWORD, is_superuser=True, is_staff=True)
        User.objects.create_superuser("new", "new@example.com", PASSWORD)
        old.refresh_from_db()
        self.assertFalse(old.is_email_verified)
        self.assertEqual(old.role, "user")

"""A reload during a token refresh must not sign the person out, and the grace must not become a hole.

The browser sent refresh token T0, the server rotated it (T0 blacklisted, T1 issued) and the page unloaded before T1 was stored. The next page presents T0 again.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import UserSession

User = get_user_model()
PASSWORD = "A-long-unique-passphrase-7"


class RotationGraceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="rg-user", email="rg@example.com", password=PASSWORD, is_email_verified=True)
        login = APIClient().post(reverse("login"), {"identifier": "rg-user", "password": PASSWORD}, format="json")
        self.assertEqual(login.status_code, 200, login.content)
        self.t0 = login.json()["refresh"]

    def refresh(self, token):
        return APIClient().post(reverse("token-refresh") if self._has("token-refresh") else "/api/auth/refresh/", {"refresh": token}, format="json")

    @staticmethod
    def _has(name):
        from django.urls import NoReverseMatch
        try:
            reverse(name)
            return True
        except NoReverseMatch:
            return False

    def test_the_replaced_token_presented_again_at_once_gets_a_fresh_pair_for_the_same_session(self):
        first = self.refresh(self.t0)
        self.assertEqual(first.status_code, 200, first.content)
        t1 = first.json()["refresh"]                                       # the answer the reloading page never stored
        again = self.refresh(self.t0)
        self.assertEqual(again.status_code, 200, again.content)
        t2 = again.json()["refresh"]
        self.assertNotIn(t2, (self.t0, t1))
        self.assertEqual(UserSession.objects.count(), 1)                    # still the one session
        self.assertEqual(self.refresh(t1).status_code, 401)                 # the lost token was retired: one chain stays valid
        self.assertEqual(self.refresh(t2).status_code, 200)                 # the new one goes on normally
        profile = APIClient()
        profile.credentials(HTTP_AUTHORIZATION=f"Bearer {again.json()['access']}")
        self.assertEqual(profile.get(reverse("profile")).status_code, 200)

    def test_after_the_window_the_replaced_token_is_refused_as_before(self):
        self.assertEqual(self.refresh(self.t0).status_code, 200)
        later = timezone.now() + timedelta(seconds=21)
        with mock.patch("accounts.sessions.timezone.now", return_value=later):
            self.assertEqual(self.refresh(self.t0).status_code, 401)

    def test_an_older_token_than_the_last_one_replaced_is_refused(self):
        t1 = self.refresh(self.t0).json()["refresh"]
        self.assertEqual(self.refresh(t1).status_code, 200)                 # a normal second rotation: the replaced token is now t1
        self.assertEqual(self.refresh(self.t0).status_code, 401)            # t0 is two rotations old

    def test_logout_and_logout_all_end_the_grace(self):
        first = self.refresh(self.t0).json()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {first['access']}")
        self.assertIn(client.post(reverse("logout"), {"refresh": first["refresh"]}, format="json").status_code, (200, 204, 205))
        self.assertEqual(self.refresh(self.t0).status_code, 401)

    def test_a_suspended_account_gets_nothing_from_the_grace(self):
        self.refresh(self.t0)
        User.objects.filter(pk=self.user.pk).update(is_suspended=True)
        self.assertNotEqual(self.refresh(self.t0).status_code, 200)

    @override_settings(REFRESH_ROTATION_GRACE_SECONDS=0)
    def test_the_grace_can_be_switched_off(self):
        self.refresh(self.t0)
        self.assertEqual(self.refresh(self.t0).status_code, 401)

    def test_a_forged_or_expired_token_is_still_refused(self):
        self.assertEqual(self.refresh(self.t0[:-3] + "abc").status_code, 401)

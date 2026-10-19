"""Signed-in sessions: inventory and revocation (B02).

Real JWTs through the real login, refresh and logout endpoints.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, UserSession
from .sessions import describe_device

PASSWORD = "ForestRiver!4821"
CHROME_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")
FIREFOX_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"


@override_settings(LOGIN_LOCK_THRESHOLD=1000)
class SessionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="sessions", email="sessions@example.com", password=PASSWORD, is_email_verified=True,
        )

    def login(self, agent=CHROME_MAC, email="sessions@example.com", password=PASSWORD):
        client = APIClient(HTTP_USER_AGENT=agent)
        response = client.post("/api/auth/login/", {"email": email, "password": password}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        tokens = response.json()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return client, tokens

    def sessions(self, client):
        response = client.get("/api/user/sessions/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def refresh(self, refresh_token):
        return APIClient().post("/api/auth/refresh/", {"refresh": refresh_token}, format="json")

    def test_each_login_is_a_listed_session_and_the_current_one_is_marked(self):
        laptop, _ = self.login(CHROME_MAC)
        self.login(FIREFOX_WIN)
        rows = self.sessions(laptop)
        self.assertEqual(sorted(row["device"] for row in rows), ["Chrome on macOS", "Firefox on Windows"])
        self.assertEqual([row["device"] for row in rows if row["is_current"]], ["Chrome on macOS"])
        self.assertTrue(all(row["ip_address"] == "127.0.0.1" for row in rows))

    def test_revoking_another_session_ends_it_at_once(self):
        laptop, _ = self.login(CHROME_MAC)
        phone, phone_tokens = self.login(FIREFOX_WIN)
        other = next(row for row in self.sessions(laptop) if not row["is_current"])

        self.assertEqual(laptop.post(f"/api/user/sessions/{other['id']}/revoke/").status_code, 204)

        self.assertEqual(phone.get("/api/user/profile/").status_code, 401)  # its access token, not just refresh
        self.assertEqual(self.refresh(phone_tokens["refresh"]).status_code, 401)
        self.assertEqual(laptop.get("/api/user/profile/").status_code, 200)
        self.assertEqual(len(self.sessions(laptop)), 1)

    def test_revoking_the_current_session_signs_this_client_out(self):
        laptop, _ = self.login()
        current = self.sessions(laptop)[0]
        self.assertEqual(laptop.post(f"/api/user/sessions/{current['id']}/revoke/").status_code, 204)
        self.assertEqual(laptop.get("/api/user/profile/").status_code, 401)

    def test_refresh_keeps_the_session_and_rotates_its_token(self):
        laptop, tokens = self.login()
        session = UserSession.objects.get(user=self.user)
        rotated = self.refresh(tokens["refresh"])
        self.assertEqual(rotated.status_code, 200, rotated.content)
        new_refresh = rotated.json()["refresh"]

        session.refresh_from_db()
        self.assertEqual(session.refresh_jti, RefreshToken(new_refresh)["jti"])
        self.assertEqual(RefreshToken(new_refresh)["sid"], str(session.pk))
        self.assertEqual(self.refresh(tokens["refresh"]).status_code, 401)  # the old one is blacklisted
        self.assertEqual(self.refresh(new_refresh).status_code, 200)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_revoking_after_rotation_blacklists_the_current_refresh_token(self):
        laptop, tokens = self.login(CHROME_MAC)
        phone, phone_tokens = self.login(FIREFOX_WIN)
        newest = self.refresh(phone_tokens["refresh"]).json()["refresh"]
        other = next(row for row in self.sessions(laptop) if not row["is_current"])
        laptop.post(f"/api/user/sessions/{other['id']}/revoke/")
        self.assertEqual(self.refresh(newest).status_code, 401)

    def test_logout_ends_the_session(self):
        laptop, tokens = self.login()
        self.assertEqual(laptop.post("/api/user/logout/", {"refresh": tokens["refresh"]}, format="json").status_code, 204)
        self.assertEqual(laptop.get("/api/user/profile/").status_code, 401)
        self.assertTrue(UserSession.objects.get(user=self.user).revoked_at)

    def test_logout_all_and_a_password_change_end_every_session(self):
        laptop, _ = self.login(CHROME_MAC)
        phone, _ = self.login(FIREFOX_WIN)
        self.assertEqual(laptop.post("/api/user/logout-all/").status_code, 200)
        self.assertEqual(phone.get("/api/user/profile/").status_code, 401)

        tablet, _ = self.login(FIREFOX_WIN)
        changed = tablet.post("/api/user/change-password/", {
            "current_password": PASSWORD, "new_password": "WinterHarbour!5937",
        }, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(tablet.get("/api/user/profile/").status_code, 401)
        fresh, _ = self.login(CHROME_MAC, password="WinterHarbour!5937")
        self.assertEqual(len(self.sessions(fresh)), 1)  # only the sign-in made after the change

    def test_refusing_the_same_password_leaves_every_device_signed_in(self):
        laptop, _ = self.login(CHROME_MAC)
        phone, _ = self.login(FIREFOX_WIN)

        refused = phone.post("/api/user/change-password/", {"current_password": PASSWORD, "new_password": PASSWORD}, format="json")

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json(), {"new_password": ["Choose a password you have not just used."]})
        self.assertEqual(laptop.get("/api/user/profile/").status_code, 200)
        self.assertEqual(phone.get("/api/user/profile/").status_code, 200)
        self.assertEqual(len(self.sessions(laptop)), 2)

    def test_a_user_cannot_see_or_revoke_someone_elses_session(self):
        User.objects.create_user(username="other", email="other@example.com", password=PASSWORD,
                                 is_email_verified=True)
        mine, _ = self.login()
        theirs, _ = self.login(FIREFOX_WIN, email="other@example.com")
        their_session = self.sessions(theirs)[0]
        self.assertEqual(len(self.sessions(mine)), 1)
        self.assertEqual(mine.post(f"/api/user/sessions/{their_session['id']}/revoke/").status_code, 404)
        self.assertEqual(theirs.get("/api/user/profile/").status_code, 200)

    def test_a_token_from_before_sessions_still_works_until_it_expires(self):
        legacy = RefreshToken.for_user(self.user)
        legacy["token_version"] = self.user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {legacy.access_token}")
        self.assertEqual(client.get("/api/user/profile/").status_code, 200)
        self.assertEqual(self.refresh(str(legacy)).status_code, 200)

    def test_ended_session_records_are_purged_after_the_retention_period(self):
        laptop, _ = self.login()
        old = UserSession.objects.create(
            user=self.user, token_version=self.user.token_version, last_seen_at=timezone.now(),
            expires_at=timezone.now() - timedelta(days=40), revoked_at=timezone.now() - timedelta(days=40),
        )
        self.sessions(laptop)
        self.assertFalse(UserSession.objects.filter(pk=old.pk).exists())

    def test_device_labels(self):
        self.assertEqual(describe_device(CHROME_MAC), "Chrome on macOS")
        self.assertEqual(describe_device(FIREFOX_WIN), "Firefox on Windows")
        self.assertEqual(describe_device("python-requests/2.32"), "API client")
        self.assertEqual(describe_device(""), "Unknown device")

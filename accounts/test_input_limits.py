"""Public endpoints refuse absurdly long identifiers, emails and passwords with a 400 and a plain sentence instead of a 500 or a huge hash (BE-001)."""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User

PASSWORD = "ForestRiver!4821"
HUGE = 300_000
MAX_IDENTIFIER_LENGTH = 254       # what the public endpoints promise (accounts/serializers.py)
MAX_PASSWORD_LENGTH = 1024


class InputLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(username="limit-user", email="limit-user@example.com", password=PASSWORD, is_email_verified=True)

    def post(self, path, body):
        return self.client.post(path, body, format="json")

    def test_a_300000_character_identifier_is_a_400_not_a_500(self):
        response = self.post("/api/auth/login/", {"identifier": "a" * HUGE, "password": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("identifier", response.json())
        self.assertIn("254", response.json()["identifier"][0])

    def test_the_other_login_fields_have_the_same_ceiling(self):
        for field in ("username", "email"):
            with self.subTest(field=field):
                value = "a" * HUGE if field == "username" else "a" * HUGE + "@example.com"
                self.assertEqual(self.post("/api/auth/login/", {field: value, "password": "x"}).status_code, 400)

    def test_an_identifier_of_exactly_the_longest_length_is_still_processed_normally(self):
        response = self.post("/api/auth/login/", {"identifier": "a" * MAX_IDENTIFIER_LENGTH, "password": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": ["Invalid username/email or password."]})            # reached the real check, which answers as for any unknown account
        self.assertEqual(self.post("/api/auth/login/", {"identifier": "a" * (MAX_IDENTIFIER_LENGTH + 1), "password": "x"}).status_code, 400)
        self.assertIn("identifier", self.post("/api/auth/login/", {"identifier": "a" * (MAX_IDENTIFIER_LENGTH + 1), "password": "x"}).json())

    def test_a_huge_password_is_refused_before_it_is_hashed(self):
        response = self.post("/api/auth/login/", {"identifier": "limit-user", "password": "p" * (MAX_PASSWORD_LENGTH + 1)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())
        self.assertIn("1024", response.json()["password"][0])

    def test_a_normal_login_still_works(self):
        for body in ({"identifier": "limit-user", "password": PASSWORD}, {"identifier": "limit-user@example.com", "password": PASSWORD}):
            with self.subTest(body=body):
                response = self.post("/api/auth/login/", body)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertIn("access", response.json())

    def test_registration_refuses_a_huge_email_and_a_huge_password(self):
        base = {"full_name": "Long Test", "email": "long-test@example.com", "password": PASSWORD}
        self.assertEqual(self.post("/api/auth/register/", {**base, "email": "a" * HUGE + "@example.com"}).status_code, 400)
        response = self.post("/api/auth/register/", {**base, "password": "p" * (MAX_PASSWORD_LENGTH + 1)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())
        self.assertEqual(self.post("/api/auth/register/", {**base, "password": PASSWORD, "password_confirm": "p" * 5000}).status_code, 400)

    def test_password_reset_refuses_a_huge_email_and_a_huge_new_password(self):
        response = self.post("/api/auth/password-reset/", {"email": "a" * HUGE + "@example.com"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())
        response = self.post("/api/auth/password-reset/confirm/", {"uidb64": "abc", "token": "t" * 50, "new_password": "p" * 5000})
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.json())
        self.assertEqual(self.post("/api/auth/password-reset/confirm/", {"uidb64": "u" * 500, "token": "t", "new_password": PASSWORD}).status_code, 400)

    def test_an_account_invitation_refuses_a_huge_password(self):
        response = self.post("/api/auth/invitations/accept/", {"token": "t" * 40, "username": "newbie", "password": "p" * 5000})
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())

    def test_an_organization_invitation_refuses_a_huge_token(self):
        self.client.force_authenticate(self.user)
        response = self.post("/api/organizations/invitations/accept/", {"token": "t" * 5000})
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.json())

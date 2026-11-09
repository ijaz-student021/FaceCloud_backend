"""Sign-in and token refresh limits that never lock out a whole network (docs/deployment/rate-limits.md)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

User = get_user_model()
PASSWORD = "ForestRiver!4821"
WRONG = "Wrong!Password9"


class SignInThrottleTests(TestCase):
    """One public address, many people."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.url = reverse("login")

    def user(self, name):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True)

    def sign_in(self, name, password=PASSWORD):
        return self.client.post(self.url, {"identifier": name, "password": password}, format="json")

    def test_a_room_of_people_signing_in_from_one_address_is_never_blocked(self):
        names = [f"student{index}" for index in range(120)]       # past the old 20-an-hour and the 100-an-hour anonymous limits
        for name in names:
            self.user(name)
        self.assertEqual({self.sign_in(name).status_code for name in names}, {200})

    def test_one_person_signing_in_again_and_again_is_not_limited_because_a_correct_password_counts_for_nothing(self):
        self.user("regular")
        self.assertEqual({self.sign_in("regular").status_code for _ in range(30)}, {200})

    def test_many_people_each_mistyping_once_are_not_blocked(self):
        names = [f"typo{index}" for index in range(40)]
        for name in names:
            self.user(name)
        self.assertEqual({self.sign_in(name, WRONG).status_code for name in names}, {400})
        self.assertEqual(self.sign_in("typo0").status_code, 200)               # and the right password still works for all of them

    @override_settings(LOGIN_LOCK_THRESHOLD=1000)                               # isolate the rate limit from the per-account lock
    def test_repeated_wrong_passwords_for_one_account_from_one_address_are_stopped_by_the_rate_limit(self):
        self.user("victim")
        codes = [self.sign_in("victim", WRONG).status_code for _ in range(22)]
        self.assertEqual(codes[:20], [400] * 20)
        self.assertEqual(codes[20:], [429, 429])
        self.assertEqual(self.sign_in("victim").status_code, 429)               # even the correct password: the guesser is refused for the hour
        self.user("someone-else")
        self.assertEqual(self.sign_in("someone-else").status_code, 200)         # while another person on the same address signs in as usual

    def test_repeated_wrong_passwords_for_one_account_lock_that_account_whatever_the_address(self):
        self.user("target")
        codes = [self.sign_in("target", WRONG).status_code for _ in range(5)]
        self.assertEqual(codes, [400] * 5)
        elsewhere = APIClient(REMOTE_ADDR="203.0.113.50")                       # a second address, same account
        locked = elsewhere.post(self.url, {"identifier": "target", "password": PASSWORD}, format="json")
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Too many failed sign-in attempts", locked.json()["detail"])

    def test_the_same_account_name_from_a_different_address_has_its_own_rate_allowance(self):
        with override_settings(LOGIN_LOCK_THRESHOLD=1000):
            self.user("shared-name")
            for _ in range(20):
                self.sign_in("shared-name", WRONG)
            self.assertEqual(self.sign_in("shared-name", WRONG).status_code, 429)
            other = APIClient(REMOTE_ADDR="198.51.100.9")
            self.assertEqual(other.post(self.url, {"identifier": "shared-name", "password": WRONG}, format="json").status_code, 400)

    @override_settings(LOGIN_LOCK_THRESHOLD=1000)
    def test_one_address_trying_many_account_names_hits_the_address_ceiling(self):
        rates = {**SimpleRateThrottle.THROTTLE_RATES, "login_ip": "5/hour"}
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", rates):
            codes = [self.sign_in(f"guess{index}", WRONG).status_code for index in range(7)]
        self.assertEqual(codes, [400] * 5 + [429] * 2)

    def test_the_login_rate_can_still_be_set_from_the_environment(self):
        rates = {**SimpleRateThrottle.THROTTLE_RATES, "login": "2/hour"}
        with override_settings(LOGIN_LOCK_THRESHOLD=1000), patch.object(SimpleRateThrottle, "THROTTLE_RATES", rates):
            self.user("tight")
            self.assertEqual([self.sign_in("tight", WRONG).status_code for _ in range(3)], [400, 400, 429])

    def test_names_are_compared_without_regard_to_case_and_spaces(self):
        with override_settings(LOGIN_LOCK_THRESHOLD=1000):
            self.user("casey")
            for variant in ("casey", "CASEY", " Casey "):
                for _ in range(7):
                    self.sign_in(variant, WRONG)
            self.assertEqual(self.sign_in("casey", WRONG).status_code, 429)          # 21 tries in all, however it was spelled

    def test_a_malformed_body_does_not_break_the_limit(self):
        for body in ({}, {"identifier": 5, "password": "x"}, {"identifier": ["a"], "password": "x"}):
            self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400)


class TokenRefreshThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.login = reverse("login")
        self.refresh = reverse("refresh")

    def tokens(self, name):
        User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True)
        return self.client.post(self.login, {"identifier": name, "password": PASSWORD}, format="json").json()["refresh"]

    def test_thirty_people_refreshing_from_one_address_each_have_their_own_allowance(self):
        refresh_tokens = [self.tokens(f"person{index}") for index in range(30)]
        codes = set()
        for _ in range(4):                                                  # 120 refreshes in all: far past the old 60 an hour per address
            for index, token in enumerate(refresh_tokens):
                response = self.client.post(self.refresh, {"refresh": token}, format="json")
                codes.add(response.status_code)
                refresh_tokens[index] = response.json().get("refresh", token)       # refresh tokens rotate: use the new one next time
        self.assertEqual(codes, {200})

    def test_one_refresh_token_used_far_too_often_is_limited(self):
        token = self.tokens("hammer")
        rates = {**SimpleRateThrottle.THROTTLE_RATES, "token_refresh": "3/hour"}
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", rates):
            codes = [self.client.post(self.refresh, {"refresh": token}, format="json").status_code for _ in range(5)]
        self.assertEqual([code == 429 for code in codes], [False, False, False, True, True])

    def test_the_address_ceiling_bounds_a_flood_of_different_tokens(self):
        rates = {**SimpleRateThrottle.THROTTLE_RATES, "token_refresh_ip": "4/hour"}
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", rates):
            codes = [self.client.post(self.refresh, {"refresh": f"not-a-token-{index}"}, format="json").status_code for index in range(6)]
        self.assertEqual(codes[:4], [401] * 4)
        self.assertEqual(codes[4:], [429, 429])

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from .lockout import (
    attempt_key,
    lock_duration,
    purge_stale_attempts,
    record_failure,
    seconds_locked,
)
from .models import AuthEvent, LoginAttempt
from .tasks import cleanup_login_attempts

User = get_user_model()
PASSWORD = "ForestRiver!4821"
WRONG = "Wrong!Password9"


class LockDurationTests(SimpleTestCase):
    @override_settings(LOGIN_LOCK_THRESHOLD=5, LOGIN_LOCK_BASE_SECONDS=30, LOGIN_LOCK_MAX_SECONDS=900)
    def test_no_lock_below_the_threshold_then_doubling_up_to_the_cap(self):
        self.assertEqual([lock_duration(n) for n in range(1, 5)], [0, 0, 0, 0])
        self.assertEqual([lock_duration(n) for n in range(5, 11)], [30, 60, 120, 240, 480, 900])
        self.assertEqual(lock_duration(500), 900)


class LoginBackoffTests(TestCase):
    def setUp(self):
        # The per-address login throttle (20/hour) keeps its counters in the cache
        # for the whole test process; these tests make far more attempts than that
        # and must neither trip it nor leave it tripped for other suites.
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username="guessme", email="guessme@example.com", password=PASSWORD,
            is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="bystander", email="bystander@example.com", password=PASSWORD,
            is_email_verified=True,
        )
        self.client = APIClient()
        self.url = reverse("login")

    def attempt(self, identifier="guessme", password=WRONG, field="username"):
        return self.client.post(self.url, {field: identifier, "password": password}, format="json")

    def fail_times(self, count, **kwargs):
        return [self.attempt(**kwargs).status_code for _ in range(count)]

    def test_the_fifth_failure_locks_and_even_the_right_password_is_then_refused(self):
        self.assertEqual(self.fail_times(5), [400] * 5)

        blocked = self.attempt(password=PASSWORD)

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Too many failed sign-in attempts", blocked.data["detail"])
        self.assertGreaterEqual(int(blocked["Retry-After"]), 1)
        self.assertLessEqual(int(blocked["Retry-After"]), 30)

    def test_four_failures_do_not_lock(self):
        self.fail_times(4)
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 200)

    def test_a_successful_login_resets_the_counter(self):
        self.fail_times(4)
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 200)
        self.assertEqual(self.fail_times(4), [400] * 4)
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 200)

    def test_the_lock_ends_and_the_right_password_works_again(self):
        self.fail_times(5)
        LoginAttempt.objects.update(locked_until=timezone.now() - timedelta(seconds=1))

        self.assertEqual(self.attempt(password=PASSWORD).status_code, 200)

    def test_each_further_failure_doubles_the_lock(self):
        self.fail_times(5)
        first = LoginAttempt.objects.get()
        LoginAttempt.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        self.attempt()  # 6th failure after the lock expired
        second = LoginAttempt.objects.get()

        first_length = (first.locked_until - first.last_failure_at).total_seconds()
        second_length = (second.locked_until - second.last_failure_at).total_seconds()
        self.assertAlmostEqual(first_length, 30, delta=1)
        self.assertAlmostEqual(second_length, 60, delta=1)

    def test_other_accounts_are_unaffected(self):
        self.fail_times(5)
        self.assertEqual(self.attempt("bystander", PASSWORD).status_code, 200)

    def test_username_and_email_of_one_account_share_one_counter(self):
        self.fail_times(3, identifier="guessme", field="username")
        self.fail_times(2, identifier="guessme@example.com", field="email")

        self.assertEqual(self.attempt(password=PASSWORD).status_code, 429)
        self.assertEqual(LoginAttempt.objects.count(), 1)

    def test_unknown_accounts_behave_exactly_like_real_ones(self):
        statuses = self.fail_times(5, identifier="ghost")
        blocked = self.attempt("ghost", PASSWORD)

        self.assertEqual(statuses, [400] * 5)
        self.assertEqual(blocked.status_code, 429)
        real = self.fail_times(5)
        real_blocked = self.attempt(password=PASSWORD)
        self.assertEqual(real, [400] * 5)
        self.assertEqual(real_blocked.status_code, blocked.status_code)
        self.assertEqual(set(real_blocked.data), set(blocked.data))

    def test_resetting_the_password_clears_the_lock(self):
        self.fail_times(5)
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 429)
        response = self.client.post(
            reverse("password-reset-confirm"),
            {
                "uidb64": urlsafe_base64_encode(force_bytes(self.user.pk)),
                "token": default_token_generator.make_token(self.user),
                "new_password": "BrandNewSecret!7351",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self.attempt(password="BrandNewSecret!7351").status_code, 200)

    def test_changing_the_password_clears_the_lock(self):
        self.client.force_authenticate(self.user)
        self.fail_times(0)
        record_failure(attempt_key(self.user, "guessme"))
        record_failure(attempt_key(self.user, "guessme"))
        LoginAttempt.objects.update(failures=9, locked_until=timezone.now() + timedelta(minutes=5))

        response = self.client.post(
            reverse("change-password"),
            {"current_password": PASSWORD, "new_password": "AnotherStrong!8392"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginAttempt.objects.count(), 0)

    def test_locking_is_audited_once_and_refused_attempts_are_logged(self):
        self.fail_times(5)
        self.attempt(password=PASSWORD)

        locks = AuthEvent.objects.filter(event="login_locked")
        self.assertEqual(locks.count(), 1)
        self.assertEqual(locks.first().details["seconds"], 30)
        self.assertTrue(AuthEvent.objects.filter(event="login_failed", details__reason="locked").exists())

    def test_the_table_holds_no_email_or_username(self):
        self.fail_times(5)
        self.fail_times(5, identifier="ghost@example.com", field="email")

        for row in LoginAttempt.objects.all():
            self.assertEqual(len(row.key_hash), 64)
            self.assertNotIn("guessme", row.key_hash)
            self.assertNotIn("ghost", row.key_hash)


class FailureWindowAndCleanupTests(TestCase):
    def setUp(self):
        self.key = attempt_key(None, "someone")

    @override_settings(LOGIN_FAILURE_WINDOW_SECONDS=900)
    def test_failures_older_than_the_window_stop_counting(self):
        start = timezone.now()
        for offset in range(4):
            record_failure(self.key, now=start + timedelta(seconds=offset))
        # A long pause, then one more failure: the old four no longer count.
        delay = record_failure(self.key, now=start + timedelta(hours=1))

        self.assertEqual(delay, 0)
        self.assertEqual(LoginAttempt.objects.get().failures, 1)

    def test_seconds_locked_reports_the_remaining_time(self):
        now = timezone.now()
        for _ in range(5):
            record_failure(self.key, now=now)
        self.assertEqual(seconds_locked(self.key, now=now), 30)
        self.assertEqual(seconds_locked(self.key, now=now + timedelta(seconds=10)), 20)
        self.assertEqual(seconds_locked(self.key, now=now + timedelta(seconds=31)), 0)

    def test_stale_counters_are_purged_but_active_locks_are_kept(self):
        now = timezone.now()
        LoginAttempt.objects.create(key_hash="a" * 64, failures=2, last_failure_at=now - timedelta(days=2))
        LoginAttempt.objects.create(
            key_hash="b" * 64, failures=9, last_failure_at=now - timedelta(days=2),
            locked_until=now + timedelta(minutes=5),
        )
        LoginAttempt.objects.create(key_hash="c" * 64, failures=1, last_failure_at=now - timedelta(hours=1))

        summary = purge_stale_attempts(now=now)

        self.assertEqual(summary, {"deleted": 1})
        self.assertEqual(
            set(LoginAttempt.objects.values_list("key_hash", flat=True)), {"b" * 64, "c" * 64},
        )
        self.assertEqual(cleanup_login_attempts()["deleted"], 0)

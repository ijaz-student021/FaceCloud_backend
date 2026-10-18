"""Security alerts: real refused requests in, alerts out, evidence kept."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuthEvent, User

from .models import SecurityAlert, SecurityEvent
from .services import evaluate_rules, purge_old_events

PASSWORD = "ForestRiver!4821"


@override_settings(SECURITY_ALERT_FAILED_LOGIN_THRESHOLD=5, SECURITY_ALERT_AUTHORIZATION_FAILURE_THRESHOLD=3,
                   SECURITY_ALERT_RATE_LIMIT_THRESHOLD=2, LOGIN_LOCK_THRESHOLD=1000,
                   CONTACT_RECIPIENT_EMAILS=["team@facecloud.test"])
class SecurityAlertTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="sec-admin", email="sec-admin@example.com",
                                              password=PASSWORD, is_email_verified=True, role="admin")
        self.user = User.objects.create_user(username="sec-user", email="sec-user@example.com",
                                             password=PASSWORD, is_email_verified=True)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

    def alerts(self, **query):
        response = self.admin_client.get("/api/system/security/alerts/", query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def test_repeated_failed_logins_open_alerts_for_the_account_and_the_address(self):
        client = APIClient()
        for _ in range(5):
            client.post("/api/auth/login/", {"email": "sec-user@example.com", "password": "wrong"}, format="json")
        rules = {(row["rule"], row["subject"]) for row in self.alerts()}
        self.assertIn(("failed_login_account", "account:sec-user@example.com"), rules)
        self.assertIn(("failed_login_address", "ip:127.0.0.1"), rules)

    def test_fewer_failures_than_the_threshold_open_nothing(self):
        client = APIClient()
        for _ in range(4):
            client.post("/api/auth/login/", {"email": "sec-user@example.com", "password": "wrong"}, format="json")
        self.assertEqual(self.alerts(), [])

    def test_403_responses_are_recorded_at_most_once_per_subject_per_second(self):
        client = APIClient()
        client.force_authenticate(self.user)
        for _ in range(10):
            self.assertEqual(client.get("/api/system/health/").status_code, 403)
        self.assertEqual(SecurityEvent.objects.filter(kind="authorization_failure",
                                                      subject=f"user:{self.user.pk}").count(), 1)

    def test_repeated_authorization_failures_open_an_alert(self):
        client = APIClient()
        client.force_authenticate(self.user)
        for second in range(3):
            moment = timezone.now() + timedelta(seconds=second)
            with patch("security.services.timezone.now", return_value=moment):
                client.get("/api/system/analytics/")
        alert = self.alerts(rule="authorization_failures")[0]
        self.assertEqual((alert["subject"], alert["event_count"], alert["status"]), (f"user:{self.user.pk}", 3, "open"))

    def test_rate_limit_violations_open_an_alert(self):
        client = APIClient()
        body = {"name": "A", "email": "a@example.org", "reason": "Other", "message": "Hello there, a real message."}
        codes = []
        for second in range(8):
            moment = timezone.now() + timedelta(seconds=second)
            with patch("security.services.timezone.now", return_value=moment):
                codes.append(client.post("/api/contact/", body, format="json").status_code)
        self.assertEqual(codes.count(429), 3)
        alert = self.alerts(rule="rate_limit_violations")[0]
        self.assertEqual(alert["subject"], "ip:127.0.0.1")

    def test_acknowledging_keeps_the_evidence_and_the_events(self):
        for _ in range(5):
            AuthEvent.objects.create(event="login_failed", identifier="x@example.com", ip_address="10.0.0.9")
        alert = next(row for row in self.alerts() if row["rule"] == "failed_login_account")
        url = f"/api/system/security/alerts/{alert['id']}/acknowledge/"

        self.assertEqual(self.admin_client.post(url, {}, format="json").status_code, 400)  # a note is required
        acknowledged = self.admin_client.post(url, {"note": "Checked: a user with a stale password manager."},
                                              format="json")
        self.assertEqual(acknowledged.status_code, 200)
        body = acknowledged.json()
        self.assertEqual((body["status"], body["acknowledged_by"]["username"]), ("acknowledged", "sec-admin"))
        self.assertEqual(len(body["evidence"]), 5)
        self.assertEqual(AuthEvent.objects.filter(event="login_failed").count(), 5)
        self.assertEqual(self.admin_client.post(url, {"note": "again"}, format="json").status_code, 409)

    def test_after_acknowledgement_only_a_new_burst_opens_a_new_alert(self):
        for _ in range(5):
            AuthEvent.objects.create(event="login_failed", identifier="y@example.com")
        evaluate_rules()
        alert = SecurityAlert.objects.get(rule="failed_login_account")
        self.admin_client.post(f"/api/system/security/alerts/{alert.pk}/acknowledge/", {"note": "seen"}, format="json")
        evaluate_rules()
        self.assertEqual(SecurityAlert.objects.filter(rule="failed_login_account", status="open").count(), 0)
        later = timezone.now() + timedelta(seconds=5)
        with patch("django.utils.timezone.now", return_value=later):
            for _ in range(5):
                AuthEvent.objects.create(event="login_failed", identifier="y@example.com")
        evaluate_rules(now=later + timedelta(seconds=1))
        self.assertEqual(SecurityAlert.objects.filter(rule="failed_login_account").count(), 2)

    def test_recording_never_changes_the_response(self):
        client = APIClient()
        client.force_authenticate(self.user)
        with patch("security.services.SecurityEvent.objects.create", side_effect=RuntimeError("db down")):
            with self.assertLogs("security.services", level="ERROR"):
                self.assertEqual(client.get("/api/system/health/").status_code, 403)

    def test_only_administrators_see_alerts_and_rules(self):
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertEqual(client.get("/api/system/security/alerts/").status_code, 403)
        rules = self.admin_client.get("/api/system/security/rules/").json()
        self.assertEqual(rules["authorization_failure_threshold"], 3)

    def test_old_refused_request_records_are_purged_but_alert_evidence_stays(self):
        SecurityEvent.objects.create(kind="rate_limited", subject="ip:1.2.3.4", status_code=429)
        SecurityEvent.objects.update(created_at=timezone.now() - timedelta(days=120))
        alert = SecurityAlert.objects.create(rule="rate_limit_violations", subject="ip:1.2.3.4", severity="warning",
                                             event_count=2, threshold=2, window_minutes=15,
                                             first_seen_at=timezone.now(), last_seen_at=timezone.now(),
                                             evidence=[{"id": 1, "source": "security"}])
        self.assertEqual(purge_old_events(), 1)
        alert.refresh_from_db()
        self.assertEqual(len(alert.evidence), 1)


class SecurityAlertReadingTests(TestCase):
    """Each alert says what kind it is, how risky, in one sentence, and which address it is about."""

    def setUp(self):
        self.admin = User.objects.create_user(username="read-admin", email="read-admin@example.com",
                                              password=PASSWORD, is_email_verified=True, role="admin")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def alert(self, **overrides):
        values = dict(rule="failed_login_address", subject="ip:198.51.100.4", severity="warning", event_count=12, threshold=10,
                      window_minutes=15, first_seen_at=timezone.now(), last_seen_at=timezone.now(), evidence=[])
        values.update(overrides)
        return SecurityAlert.objects.create(**values)

    def row(self):
        response = self.client.get("/api/system/security/alerts/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"][0]

    def test_a_failed_login_burst_reads_as_a_sentence_with_its_numbers(self):
        self.alert()
        row = self.row()
        self.assertEqual((row["type"], row["risk"], row["ip_address"]), ("failed_login", "medium", "198.51.100.4"))
        self.assertEqual(row["message"], "12 failed sign-ins from address 198.51.100.4 in 15 minutes (alert at 10).")

    def test_three_times_over_the_threshold_is_high_risk(self):
        self.alert(severity="high", event_count=31)
        self.assertEqual(self.row()["risk"], "high")

    def test_each_rule_maps_to_one_screen_type(self):
        for rule, expected in (("failed_login_account", "failed_login"), ("authorization_failures", "authorization_failure"),
                               ("rate_limit_violations", "rate_limit")):
            SecurityAlert.objects.all().delete()
            self.alert(rule=rule, subject="user:7")
            self.assertEqual(self.row()["type"], expected, rule)

    def test_an_alert_about_a_user_takes_its_address_from_the_evidence_or_has_none(self):
        self.alert(rule="authorization_failures", subject="user:7", evidence=[{"id": 1, "source": "security", "ip_address": "203.0.113.9"}])
        self.assertEqual(self.row()["ip_address"], "203.0.113.9")
        SecurityAlert.objects.all().delete()
        self.alert(rule="authorization_failures", subject="user:7", evidence=[])
        self.assertIsNone(self.row()["ip_address"])
        self.assertIn("user 7", self.row()["message"])

    def test_the_old_fields_are_still_there(self):
        self.alert()
        for key in ("id", "rule", "subject", "severity", "status", "event_count", "threshold", "window_minutes",
                    "first_seen_at", "last_seen_at", "acknowledged_by", "acknowledged_at", "acknowledgement_note", "created_at"):
            self.assertIn(key, self.row())

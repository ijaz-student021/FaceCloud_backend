"""The timeline entries the dashboard draws: id, type, message, timestamp and status, with the old keys kept."""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import AuthEvent

from .models import ActivityEvent
from .presentation import CATEGORY_DEFAULT_TYPE, EXACT, GROUP_MESSAGES, describe
from .services import record_activity_event

User = get_user_model()

TYPES = {"upload", "enhance", "validate", "download", "live", "group", "reject", "login", "email", "admin"}
STATUSES = {"success", "warning", "error", "info"}
OLD_KEYS = {"source", "category", "event", "object_type", "object_id", "identifier", "ip_address", "details", "created_at"}


class MappingTests(SimpleTestCase):
    def test_every_mapped_event_uses_only_the_allowed_types_and_statuses(self):
        for event, (kind, status, message) in EXACT.items():
            self.assertIn(kind, TYPES, event)
            self.assertIn(status, STATUSES, event)
            self.assertTrue(message.endswith("."), event)

    def test_every_authentication_event_the_backend_can_record_is_mapped(self):
        for event, _ in AuthEvent.EVENT_CHOICES:
            if event.startswith("two_factor"):
                continue  # two-factor sign-in was removed; old rows fall back to the generic reading
            self.assertIn(event, EXACT, event)

    def test_the_event_names_the_code_records_are_all_mapped(self):
        recorded = {
            ("upload", "upload_received"), ("processing", "processing_started"),
            *(("processing", f"batch_{s}") for s in ("completed", "partial", "failed", "cancelled")),
            *(("processing", f"quality_{d}") for d in ("accepted", "rejected", "needs_review", "failed")),
            *(("processing", f"enhancement_{s}") for s in ("completed", "failed", "cancelled")),
            *(("download", f"package_{s}") for s in ("ready", "failed", "expired")),
            *(("download", f"identity_export_{s}") for s in ("ready", "failed", "expired")),
            *(("download", f"report_{s}") for s in ("ready", "failed")),
            ("share", "share_link_created"), ("share", "share_link_revoked"),
            ("privacy", "consent_granted"), ("privacy", "consent_withdrawn"),
            ("privacy", "facial_data_erased"), ("privacy", "data_exported"),
        }
        for category, event in recorded:
            self.assertIn(event, EXACT, f"{category}/{event} has no explicit reading")
        for action in ("renamed", "merged", "moved", "removed", "profile_set", "deleted", "reviewed"):
            self.assertEqual(describe("group", f"group_{action}")[0], "group")
            self.assertIn(action, GROUP_MESSAGES)

    def test_representative_readings(self):
        self.assertEqual(describe("processing", "enhancement_completed")[:2], ("enhance", "success"))
        self.assertEqual(describe("processing", "enhancement_failed")[:2], ("enhance", "error"))
        self.assertEqual(describe("processing", "quality_rejected")[:2], ("reject", "warning"))
        self.assertEqual(describe("processing", "batch_partial")[:2], ("enhance", "warning"))
        self.assertEqual(describe("auth", "login")[:2], ("login", "success"))
        self.assertEqual(describe("auth", "login_failed")[:2], ("login", "error"))
        self.assertEqual(describe("auth", "email_verified")[:2], ("email", "success"))
        self.assertEqual(describe("download", "package_ready")[:2], ("download", "success"))
        self.assertEqual(describe("privacy", "consent_withdrawn")[:2], ("validate", "warning"))

    def test_an_unknown_event_still_reads_sensibly(self):
        for category in CATEGORY_DEFAULT_TYPE:
            kind, status, message = describe(category, "something_new_failed")
            self.assertIn(kind, TYPES)
            self.assertEqual(status, "error")
            self.assertEqual(message, "Something new failed.")
        self.assertEqual(describe("moderation", "item_rejected")[0], "reject")
        self.assertEqual(describe("admin", "settings_changed"), ("admin", "info", "Settings changed."))


class TimelineApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="timeline", email="timeline@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="timeline-other", email="timeline-other@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("user-activity")

    def test_every_entry_has_the_new_and_the_old_keys(self):
        record_activity_event(self.user, "processing", "enhancement_completed", object_type="ImageEnhancementResult", object_id=4)
        AuthEvent.objects.create(user=self.user, event="login", identifier="timeline@example.com")
        body = self.client.get(self.url).json()
        self.assertEqual(set(body), {"page", "page_size", "has_next", "results"})
        self.assertEqual(len(body["results"]), 2)
        for entry in body["results"]:
            self.assertTrue(OLD_KEYS <= set(entry), entry)
            self.assertTrue({"id", "type", "message", "timestamp", "status"} <= set(entry), entry)
            self.assertIn(entry["type"], TYPES)
            self.assertIn(entry["status"], STATUSES)
            self.assertEqual(entry["timestamp"], entry["created_at"])

    def test_ids_are_unique_stable_and_distinguish_the_two_sources(self):
        activity = record_activity_event(self.user, "upload", "upload_received")
        auth = AuthEvent.objects.create(user=self.user, event="login")
        ids = {entry["id"] for entry in self.client.get(self.url).json()["results"]}
        self.assertEqual(ids, {f"activity-{activity.pk}", f"auth-{auth.pk}"})
        again = {entry["id"] for entry in self.client.get(self.url).json()["results"]}
        self.assertEqual(ids, again)

    def test_a_failed_login_reads_as_an_error_and_a_passed_check_as_success(self):
        AuthEvent.objects.create(user=self.user, event="login_failed")
        record_activity_event(self.user, "processing", "quality_accepted")
        by_event = {entry["event"]: entry for entry in self.client.get(self.url).json()["results"]}
        self.assertEqual((by_event["login_failed"]["type"], by_event["login_failed"]["status"]), ("login", "error"))
        self.assertEqual((by_event["quality_accepted"]["type"], by_event["quality_accepted"]["status"]), ("validate", "success"))
        self.assertEqual(by_event["quality_accepted"]["message"], "A photo passed the quality check.")

    def test_pagination_and_the_category_filter_still_work(self):
        for _ in range(5):
            record_activity_event(self.user, "upload", "upload_received")
        AuthEvent.objects.create(user=self.user, event="login")
        first = self.client.get(self.url, {"page_size": 4}).json()
        self.assertEqual((len(first["results"]), first["has_next"]), (4, True))
        second = self.client.get(self.url, {"page_size": 4, "page": 2}).json()
        self.assertEqual((len(second["results"]), second["has_next"]), (2, False))
        only_auth = self.client.get(self.url, {"category": "auth"}).json()
        self.assertEqual({entry["type"] for entry in only_auth["results"]}, {"login"})

    def test_an_empty_timeline_is_an_empty_page(self):
        body = self.client.get(self.url).json()
        self.assertEqual((body["results"], body["has_next"]), ([], False))

    def test_other_users_entries_never_appear(self):
        record_activity_event(self.other, "upload", "upload_received")
        AuthEvent.objects.create(user=self.other, event="login")
        self.assertEqual(self.client.get(self.url).json()["results"], [])
        self.assertEqual(ActivityEvent.objects.filter(user=self.other).count(), 1)

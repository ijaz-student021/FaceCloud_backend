from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuthEvent
from activity.models import ActivityEvent

from .audit import InvalidAuditFilter, build_audit_feed

User = get_user_model()


class BuildAuditFeedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="audit-user", email="audit-user@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="audit-other", email="audit-other@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )

    def auth_event(self, **kwargs):
        return AuthEvent.objects.create(user=self.user, event="login", **kwargs)

    def activity_event(self, **kwargs):
        return ActivityEvent.objects.create(
            user=self.user, category=ActivityEvent.Category.UPLOAD, event="upload_received", **kwargs
        )

    def test_both_sources_are_merged_newest_first(self):
        auth = self.auth_event()
        AuthEvent.objects.filter(pk=auth.pk).update(created_at=timezone.now() - timezone.timedelta(minutes=5))
        activity = self.activity_event()

        result = build_audit_feed()

        sources = [(row["source"], row["id"]) for row in result["results"]]
        self.assertEqual(sources[:2], [("activity", activity.pk), ("auth", auth.pk)])

    def test_user_filter_restricts_both_sources(self):
        self.auth_event()
        self.activity_event()
        AuthEvent.objects.create(user=self.other, event="login")
        ActivityEvent.objects.create(user=self.other, category=ActivityEvent.Category.UPLOAD, event="upload_received")

        result = build_audit_feed(user=str(self.user.pk))

        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(row["user_id"] == self.user.pk for row in result["results"]))

    def test_source_filter_selects_only_that_table(self):
        self.auth_event()
        self.activity_event()

        auth_only = build_audit_feed(source="auth")
        activity_only = build_audit_feed(source="activity")

        self.assertEqual([row["source"] for row in auth_only["results"]], ["auth"])
        self.assertEqual([row["source"] for row in activity_only["results"]], ["activity"])

    def test_an_unknown_source_is_rejected(self):
        with self.assertRaises(InvalidAuditFilter):
            build_audit_feed(source="not-a-real-source")

    def test_category_auth_selects_only_auth_events(self):
        self.auth_event()
        self.activity_event()

        result = build_audit_feed(category="auth")

        self.assertEqual([row["source"] for row in result["results"]], ["auth"])

    def test_category_filters_activity_events_by_category(self):
        self.activity_event()
        ActivityEvent.objects.create(user=self.user, category=ActivityEvent.Category.SHARE, event="share_link_created")

        result = build_audit_feed(category="share")

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["category"], "share")

    def test_an_unknown_category_is_rejected(self):
        with self.assertRaises(InvalidAuditFilter):
            build_audit_feed(category="not-a-real-category")

    def test_pagination_splits_merged_results_correctly(self):
        for _ in range(5):
            self.activity_event()

        first = build_audit_feed(page=1, page_size=2)
        second = build_audit_feed(page=2, page_size=2)

        self.assertEqual(len(first["results"]), 2)
        self.assertTrue(first["has_next"])
        self.assertNotEqual(
            {row["id"] for row in first["results"]}, {row["id"] for row in second["results"]},
        )

    def test_has_next_is_false_on_the_last_page(self):
        self.activity_event()

        result = build_audit_feed(page=1, page_size=50)

        self.assertFalse(result["has_next"])

    def test_a_non_numeric_user_is_rejected(self):
        with self.assertRaises(InvalidAuditFilter):
            build_audit_feed(user="not-an-id")

    def test_details_and_object_references_are_preserved(self):
        self.activity_event(object_type="UploadBatch", object_id="7", details={"upload_type": "single"})

        result = build_audit_feed(category="upload")

        row = result["results"][0]
        self.assertEqual(row["object_type"], "UploadBatch")
        self.assertEqual(row["object_id"], "7")
        self.assertEqual(row["details"], {"upload_type": "single"})


class AdminAuditEndpointTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="audit-admin", email="audit-admin@example.com",
            password="ForestRiver!4821", is_email_verified=True, role="admin",
        )
        self.plain = User.objects.create_user(
            username="audit-plain", email="audit-plain@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.url = reverse("operations:admin-audit")

    def test_requires_authentication_and_admin_role(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.plain)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_sees_real_events(self):
        AuthEvent.objects.create(user=self.admin, event="login")
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["results"]), 1)

    def test_invalid_filter_is_400(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url, {"source": "nowhere"})
        self.assertEqual(response.status_code, 400)

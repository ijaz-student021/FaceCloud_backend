from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import AuthEvent
from accounts.services import record_auth_event

from .models import ActivityEvent
from .services import get_user_timeline, record_activity_event

User = get_user_model()


class RecordActivityEventTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", email="alice@example.com", password="pw12345!")

    def test_creates_event_with_expected_fields(self):
        event = record_activity_event(
            self.user, "upload", "upload_received",
            object_type="UploadBatch", object_id=7,
            details={"upload_type": "single"},
        )
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.category, "upload")
        self.assertEqual(event.event, "upload_received")
        self.assertEqual(event.object_type, "UploadBatch")
        self.assertEqual(event.object_id, "7")
        self.assertEqual(event.details, {"upload_type": "single"})

    def test_object_id_defaults_to_empty_string(self):
        event = record_activity_event(self.user, "group", "group_renamed")
        self.assertEqual(event.object_id, "")


class GetUserTimelineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bob", email="bob@example.com", password="pw12345!")
        self.other = User.objects.create_user(username="carl", email="carl@example.com", password="pw12345!")

    def test_merges_activity_and_auth_events_by_recency(self):
        record_activity_event(self.user, "upload", "upload_received")
        record_auth_event("login", user=self.user, identifier=self.user.email)
        record_activity_event(self.user, "processing", "batch_completed")

        timeline = get_user_timeline(self.user, page=1, page_size=10)

        self.assertEqual(len(timeline["results"]), 3)
        created_ats = [item["created_at"] for item in timeline["results"]]
        self.assertEqual(created_ats, sorted(created_ats, reverse=True))
        sources = {item["source"] for item in timeline["results"]}
        self.assertEqual(sources, {"activity", "auth"})

    def test_excludes_other_users_events(self):
        record_activity_event(self.other, "upload", "upload_received")
        timeline = get_user_timeline(self.user, page=1, page_size=10)
        self.assertEqual(timeline["results"], [])

    def test_category_filter_restricts_to_one_source(self):
        record_activity_event(self.user, "upload", "upload_received")
        record_auth_event("login", user=self.user, identifier=self.user.email)

        auth_only = get_user_timeline(self.user, category="auth", page=1, page_size=10)
        self.assertEqual(len(auth_only["results"]), 1)
        self.assertEqual(auth_only["results"][0]["source"], "auth")

        upload_only = get_user_timeline(self.user, category="upload", page=1, page_size=10)
        self.assertEqual(len(upload_only["results"]), 1)
        self.assertEqual(upload_only["results"][0]["source"], "activity")

    def test_pagination_has_next(self):
        for index in range(5):
            record_activity_event(self.user, "upload", f"event_{index}")
        first_page = get_user_timeline(self.user, page=1, page_size=2)
        self.assertEqual(len(first_page["results"]), 2)
        self.assertTrue(first_page["has_next"])

        last_page = get_user_timeline(self.user, page=3, page_size=2)
        self.assertEqual(len(last_page["results"]), 1)
        self.assertFalse(last_page["has_next"])


class UserActivityTimelineViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dana", email="dana@example.com", password="pw12345!",
            is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="erin", email="erin@example.com", password="pw12345!",
            is_email_verified=True,
        )
        self.url = reverse("user-activity")

    def _auth(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_returns_only_the_requesting_users_events(self):
        record_activity_event(self.user, "upload", "upload_received")
        record_activity_event(self.other, "upload", "upload_received")

        self._auth(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)

    def test_rejects_unsupported_category(self):
        self._auth(self.user)
        response = self.client.get(self.url, {"category": "not-a-real-category"})
        self.assertEqual(response.status_code, 400)

    def test_rejects_non_integer_page(self):
        self._auth(self.user)
        response = self.client.get(self.url, {"page": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_accepts_valid_category_filter(self):
        record_activity_event(self.user, "group", "group_renamed")
        record_activity_event(self.user, "upload", "upload_received")

        self._auth(self.user)
        response = self.client.get(self.url, {"category": "group"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["category"], "group")

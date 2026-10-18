"""``?type=`` on the activity timeline: the database filter must pick exactly the entries whose ``type`` reads that way."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import AuthEvent

from .models import ActivityEvent
from .presentation import CATEGORY_DEFAULT_TYPE, EXACT, TYPES, describe, events_of_type

User = get_user_model()

UNKNOWN_NAMES = ["brand_new_event", "photo_rejected_by_robot", "weird_reject_case", "group_something_new", "mystery"]


class FilterAgreesWithDescribeTests(TestCase):
    """The mapping lives in ``describe``; the filter is derived from the same table, and this test keeps them from drifting."""

    def setUp(self):
        self.user = User.objects.create_user(username="typed", email="typed@example.com", password="x")

    def test_for_every_name_family_and_category_the_filter_equals_describe(self):
        names = list(EXACT) + UNKNOWN_NAMES + ["group_renamed", "group_merged", "group_zzz"]
        for category in ActivityEvent.Category.values:
            for name in names:
                ActivityEvent.objects.create(user=self.user, category=category, event=name)
        for kind in TYPES:
            expected = {
                (e.category, e.event) for e in ActivityEvent.objects.all() if describe(e.category, e.event)[0] == kind
            }
            got = set(ActivityEvent.objects.filter(events_of_type(kind)).values_list("category", "event"))
            self.assertEqual(got, expected, f"activity type {kind}")

    def test_the_same_holds_for_the_account_trail(self):
        for name, _label in AuthEvent.EVENT_CHOICES:
            AuthEvent.objects.create(user=self.user, event=name)
        for kind in TYPES:
            expected = {e.event for e in AuthEvent.objects.all() if describe("auth", e.event)[0] == kind}
            got = set(AuthEvent.objects.filter(events_of_type(kind, auth=True)).values_list("event", flat=True))
            self.assertEqual(got, expected, f"account type {kind}")

    def test_every_default_type_is_a_known_type(self):
        self.assertTrue(set(CATEGORY_DEFAULT_TYPE.values()) <= set(TYPES))
        self.assertTrue({kind for kind, _s, _m in EXACT.values()} <= set(TYPES))


class TimelineTypeParameterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tl", email="tl@example.com", password="x", is_email_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        for _ in range(3):
            ActivityEvent.objects.create(user=self.user, category="upload", event="upload_received")
        ActivityEvent.objects.create(user=self.user, category="processing", event="enhancement_completed")
        ActivityEvent.objects.create(user=self.user, category="moderation", event="quality_rejected")
        AuthEvent.objects.create(user=self.user, event="login")
        AuthEvent.objects.create(user=self.user, event="login_failed")

    def entries(self, **query):
        response = self.client.get("/api/user/activity/", query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def test_each_type_returns_only_its_entries_and_all_of_them(self):
        self.assertEqual({e["type"] for e in self.entries(type="upload")}, {"upload"})
        self.assertEqual(len(self.entries(type="upload")), 3)
        self.assertEqual(len(self.entries(type="login")), 2)                   # account events join the merge
        self.assertEqual([e["event"] for e in self.entries(type="reject")], ["quality_rejected"])
        self.assertEqual(self.entries(type="group"), [])                       # none is an empty page, not an error

    def test_it_combines_with_the_category_filter_and_with_paging(self):
        self.assertEqual(len(self.entries(type="upload", category="upload")), 3)
        self.assertEqual(self.entries(type="upload", category="processing"), [])
        page = self.client.get("/api/user/activity/", {"type": "upload", "page_size": 2}).json()
        self.assertEqual((len(page["results"]), page["has_next"]), (2, True))

    def test_an_unknown_type_is_a_400_that_lists_the_choices(self):
        response = self.client.get("/api/user/activity/", {"type": "sleepy"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("upload", str(response.json()["type"]))

    def test_without_it_nothing_changes(self):
        self.assertEqual(len(self.entries()), 7)

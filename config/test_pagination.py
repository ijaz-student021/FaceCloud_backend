"""Opt-in pagination: the envelope on request, the plain array otherwise."""
import warnings

from django.core.paginator import UnorderedObjectListWarning
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from uploads.models import UploadBatch

# Every list endpoint without URL parameters, for the signed-in user.
PLAIN_LIST_PATHS = (
    "/api/images/batches/", "/api/images/", "/api/enhancements/jobs/", "/api/enhancements/results/",
    "/api/enhancements/comparisons/", "/api/quality/results/", "/api/duplicates/results/",
    "/api/identities/groups/", "/api/identities/review/", "/api/identities/history/",
    "/api/reports/", "/api/downloads/", "/api/downloads/packages/", "/api/notifications/",
    "/api/shares/", "/api/organizations/",
)


class OptionalPaginationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="pager", email="pager@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.make_batches(25)

    def make_batches(self, count):
        UploadBatch.objects.bulk_create([UploadBatch(owner=self.user, upload_type="single") for _ in range(count)])

    def test_without_page_parameters_the_plain_array_is_unchanged(self):
        body = self.client.get("/api/images/batches/").json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 25)

    def test_page_returns_the_envelope(self):
        body = self.client.get("/api/images/batches/?page=1").json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual((body["count"], len(body["results"])), (25, 20))
        second = self.client.get("/api/images/batches/?page=2").json()
        self.assertEqual(len(second["results"]), 5)
        self.assertFalse({row["id"] for row in body["results"]} & {row["id"] for row in second["results"]})

    def test_page_size_is_honoured_and_capped(self):
        self.assertEqual(len(self.client.get("/api/images/batches/?page_size=7").json()["results"]), 7)
        self.make_batches(120)
        self.assertEqual(len(self.client.get("/api/images/batches/?page_size=500").json()["results"]), 100)

    def test_a_page_past_the_end_is_404(self):
        self.assertEqual(self.client.get("/api/images/batches/?page=9").status_code, 404)

    def test_every_list_paginates_on_a_stable_order(self):
        for path in PLAIN_LIST_PATHS:
            with self.subTest(path=path), warnings.catch_warnings():
                warnings.simplefilter("error", UnorderedObjectListWarning)
                response = self.client.get(f"{path}?page=1")
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertIn("results", response.json())

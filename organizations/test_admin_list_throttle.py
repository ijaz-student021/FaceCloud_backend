"""Reading the administrator's organization list is not limited to 30 an hour; creating an organization still is (BE-004)."""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User

PASSWORD = "ForestRiver!4821"


class AdminListThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="thr-admin", email="thr-admin@example.com", password=PASSWORD, is_email_verified=True, role="admin")
        self.owner = User.objects.create_user(username="thr-owner", email="thr-owner@example.com", password=PASSWORD, is_email_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = reverse("organizations:organization-admin-list-create")

    def test_sixty_reads_in_a_row_are_never_throttled(self):
        statuses = [self.client.get(self.url).status_code for _ in range(60)]
        self.assertEqual(set(statuses), {200}, statuses)

    def test_reading_does_not_use_up_the_creation_allowance(self):
        for _ in range(40):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        first = self.client.post(self.url, {"name": "After the reads", "owner_id": self.owner.pk}, format="json")
        self.assertEqual(first.status_code, 201, first.content)

    def test_the_thirty_first_creation_in_an_hour_is_throttled(self):
        statuses = [self.client.post(self.url, {"name": f"Org {number}", "owner_id": self.owner.pk}, format="json").status_code for number in range(31)]
        self.assertEqual(statuses[:30], [201] * 30, statuses)
        self.assertEqual(statuses[30], 429)
        self.assertEqual(self.client.get(self.url).status_code, 200)                      # the list is still readable while creating is throttled

    def test_the_throttled_answer_is_the_usual_one(self):
        for number in range(30):
            self.client.post(self.url, {"name": f"Org {number}", "owner_id": self.owner.pk}, format="json")
        response = self.client.post(self.url, {"name": "One too many", "owner_id": self.owner.pk}, format="json")
        self.assertEqual(response.status_code, 429)
        self.assertIn("throttled", response.json()["detail"].lower())

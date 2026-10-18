"""GET /api/user/admin/users/: search, role and status filters, and the number of photos each account uploaded."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from uploads.models import ImageUpload, UploadBatch

User = get_user_model()
PASSWORD = "Ocean!Breeze2026x"


class AdminUserListTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.sara = User.objects.create_user("sara", "sara@example.com", PASSWORD, is_email_verified=True, first_name="Sara", last_name="Khan")
        self.omar = User.objects.create_user("omar", "omar@example.com", PASSWORD, is_email_verified=True, role="moderator")
        self.client.force_authenticate(self.admin)
        self.url = reverse("admin-user-list")

    def upload(self, owner, count):
        batch = UploadBatch.objects.create(owner=owner, upload_type="multiple", total_files=count)
        for index in range(count):
            ImageUpload.objects.create(owner=owner, batch=batch, original_name=f"{index}.jpg", file_extension=".jpg", mime_type="image/jpeg",
                                       file_size=10, width=10, height=10)

    def rows(self, **query):
        response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    def test_each_row_says_how_many_photos_the_account_uploaded(self):
        self.upload(self.sara, 3)
        self.upload(self.omar, 1)
        counts = {row["username"]: row["uploads_count"] for row in self.rows()}
        self.assertEqual(counts, {"boss": 0, "sara": 3, "omar": 1})

    def test_the_count_costs_one_query_for_the_whole_list(self):
        for owner in (self.sara, self.omar):
            self.upload(owner, 2)
        with self.assertNumQueries(1):         # one query for the whole page, counts included
            self.client.get(self.url)

    def test_the_count_is_also_right_on_the_response_to_a_change(self):
        self.upload(self.sara, 2)
        response = self.client.patch(reverse("admin-user-update", args=[self.sara.pk]), {"is_suspended": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["uploads_count"], 2)

    def test_last_sign_in_is_included(self):
        self.assertIn("last_login", self.rows()[0])

    def test_search_matches_username_email_and_name(self):
        for needle in ("sar", "SARA@EXAMPLE", "khan"):
            self.assertEqual([row["username"] for row in self.rows(q=needle)], ["sara"], needle)

    def test_filter_by_role_and_by_status(self):
        User.objects.filter(pk=self.sara.pk).update(is_suspended=True)
        self.assertEqual([row["username"] for row in self.rows(role="moderator")], ["omar"])
        self.assertEqual([row["username"] for row in self.rows(status="suspended")], ["sara"])
        self.assertEqual(sorted(row["username"] for row in self.rows(status="active")), ["boss", "omar"])

    def test_an_unknown_role_or_status_is_a_clear_400(self):
        response = self.client.get(self.url, {"role": "wizard"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("role", response.json())
        self.assertEqual(self.client.get(self.url, {"status": "sleepy"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_without_page_the_list_is_still_a_plain_array_and_with_page_it_is_paginated(self):
        self.assertIsInstance(self.rows(), list)
        paged = self.rows(page=1, page_size=2)
        self.assertEqual((paged["count"], len(paged["results"])), (3, 2))

    def test_only_an_administrator_may_list(self):
        self.client.force_authenticate(self.sara)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)


class LastSignInTests(APITestCase):
    """`last_login` was null for everyone because the custom sign-in serializer skipped SimpleJWT's update."""

    def setUp(self):
        self.user = User.objects.create_user("lastin", "lastin@example.com", PASSWORD, is_email_verified=True)

    def sign_in(self, password=PASSWORD):
        return self.client.post(reverse("login"), {"identifier": "lastin", "password": password}, format="json")

    def test_a_successful_sign_in_stamps_last_login(self):
        self.assertIsNone(User.objects.get(pk=self.user.pk).last_login)
        self.assertEqual(self.sign_in().status_code, status.HTTP_200_OK)
        stamped = User.objects.get(pk=self.user.pk).last_login
        self.assertIsNotNone(stamped)
        self.assertLess((timezone.now() - stamped).total_seconds(), 30)

    def test_a_failed_sign_in_does_not(self):
        self.assertEqual(self.sign_in("wrong-password").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(User.objects.get(pk=self.user.pk).last_login)

    def test_a_token_refresh_does_not_count_as_signing_in(self):
        tokens = self.sign_in().json()
        first = User.objects.get(pk=self.user.pk).last_login
        self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(User.objects.get(pk=self.user.pk).last_login, first)

    def test_the_administrator_list_shows_it(self):
        self.sign_in()
        admin = User.objects.create_superuser("boss2", "boss2@example.com", PASSWORD)
        self.client.force_authenticate(admin)
        rows = {row["username"]: row for row in self.client.get(reverse("admin-user-list")).json()}
        self.assertIsNotNone(rows["lastin"]["last_login"])

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from identities.models import IdentityGroup
from uploads.models import ImageUpload, UploadBatch

User = get_user_model()


class SearchTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username="searcher", email="searcher@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="bystander", email="bystander@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("search")

    def batch(self, owner, name="Holiday upload"):
        return UploadBatch.objects.create(owner=owner, upload_type="multiple", display_name=name, total_files=3)

    def image(self, owner, name, batch=None, status="uploaded"):
        return ImageUpload.objects.create(
            owner=owner, batch=batch or self.batch(owner, f"batch for {name}"), original_name=name, status=status,
            original_file=f"users/{owner.pk}/originals/{name}",
        )

    def group(self, owner, name, members=2, state=IdentityGroup.State.ACTIVE):
        return IdentityGroup.objects.create(
            owner=owner, display_name=name, member_count=members, state=state,
            embedding_dimension=3, provider="test", policy_version="v1",
        )

    def search(self, **params):
        return self.client.get(self.url, params)

    def zip_batch(self, owner, archive, **values):
        return UploadBatch.objects.create(owner=owner, upload_type="zip", original_archive_name=archive, total_files=5, **values)

    def test_a_zip_is_found_by_its_archive_name_and_labelled_with_it(self):
        batch = self.zip_batch(self.user, "wedding-photos.zip")
        jobs = self.search(q="wedding").json()["jobs"]
        self.assertEqual([job["id"] for job in jobs], [batch.pk])
        self.assertEqual(jobs[0]["display_name"], "wedding-photos.zip")
        self.assertEqual(set(jobs[0]), {"id", "display_name", "status", "total_files", "created_at"})
        self.assertEqual(self.search(q="WEDDING-PHOTOS.ZIP").json()["jobs"][0]["id"], batch.pk)

    def test_a_renamed_batch_is_found_by_its_name_and_the_name_wins_over_the_archive(self):
        batch = self.zip_batch(self.user, "IMG_2024.zip", display_name="Summer trip")
        found = self.search(q="summer").json()["jobs"]
        self.assertEqual((found[0]["id"], found[0]["display_name"]), (batch.pk, "Summer trip"))
        # The archive's own file name still finds it, and the label is still the chosen name.
        by_archive = self.search(q="img_2024").json()["jobs"]
        self.assertEqual((by_archive[0]["id"], by_archive[0]["display_name"]), (batch.pk, "Summer trip"))

    def test_a_batch_matching_both_fields_is_listed_once(self):
        self.zip_batch(self.user, "party.zip", display_name="Party night")
        self.assertEqual(len(self.search(q="part").json()["jobs"]), 1)

    def test_an_unnamed_batch_is_never_blank(self):
        from search.views import job_label

        single = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.assertEqual(job_label(single), f"Upload {single.pk}")
        self.assertEqual(job_label(self.zip_batch(self.user, "a.zip")), "a.zip")
        self.assertEqual(job_label(self.batch(self.user, "Named")), "Named")

    def test_single_uploads_without_a_name_are_not_matched_by_their_label(self):
        UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.assertEqual(self.search(q="upload").json()["jobs"], [])

    def test_archive_names_of_other_users_are_never_searched(self):
        self.zip_batch(self.other, "wedding-theirs.zip")
        self.assertEqual(self.search(q="wedding").json()["jobs"], [])

    def test_wildcards_are_literal_in_archive_names_too(self):
        self.zip_batch(self.user, "plain.zip")
        literal = self.zip_batch(self.user, "100%_done.zip")
        for wildcard in ("%%", "__", "p%n", "_l"):
            self.assertEqual(self.search(q=wildcard).json()["jobs"], [], wildcard)
        self.assertEqual([job["id"] for job in self.search(q="0%_d").json()["jobs"]], [literal.pk])

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(self.url, {"q": "beach"}).status_code, 401)

    def test_each_kind_is_returned_with_its_documented_fields(self):
        batch = self.batch(self.user, "Beach weekend")
        image = self.image(self.user, "beach-01.jpg", batch=batch)
        group = self.group(self.user, "Beach friends", members=4)

        body = self.search(q="beach").json()

        self.assertEqual(body["query"], "beach")
        self.assertEqual(set(body), {"query", "images", "groups", "people", "albums", "places", "jobs"})   # "people", "albums" and "places" were added; nothing was removed
        self.assertEqual(
            set(body["images"][0]), {"id", "name", "thumbnail_url", "status", "created_at"},
        )
        self.assertEqual(body["images"][0]["id"], image.pk)
        self.assertEqual(body["images"][0]["name"], "beach-01.jpg")
        self.assertTrue(body["images"][0]["thumbnail_url"].endswith(f"/api/images/{image.pk}/thumbnail/"))
        self.assertEqual(body["groups"], [{"id": group.pk, "display_name": "Beach friends", "member_count": 4}])
        self.assertEqual(set(body["jobs"][0]), {"id", "display_name", "status", "total_files", "created_at"})
        self.assertEqual(body["jobs"][0]["id"], batch.pk)
        self.assertEqual(body["jobs"][0]["display_name"], "Beach weekend")
        self.assertEqual(body["jobs"][0]["total_files"], 3)

    def test_matching_is_case_insensitive_and_contains(self):
        self.image(self.user, "My-BEACH-day.JPG")
        self.group(self.user, "Aunt Sandra")
        self.assertEqual(len(self.search(q="beach").json()["images"]), 1)
        self.assertEqual(len(self.search(q="DAY.jp").json()["images"]), 1)
        self.assertEqual(len(self.search(q="SANDR").json()["groups"]), 1)

    def test_like_wildcards_are_treated_as_literal_characters(self):
        self.image(self.user, "plain-photo.jpg")
        self.image(self.user, "100%_sure.jpg")
        self.group(self.user, "Person One")
        for wildcard in ("%%", "__", "p%o", "_l", "%"):
            if len(wildcard) < 2:
                continue
            body = self.search(q=wildcard).json()
            self.assertEqual(body["images"], [], wildcard)
            self.assertEqual(body["groups"], [], wildcard)
        self.assertEqual([i["name"] for i in self.search(q="0%_s").json()["images"]], ["100%_sure.jpg"])

    def test_only_the_callers_records_are_returned(self):
        self.image(self.other, "beach-theirs.jpg")
        self.group(self.other, "Beach crowd")
        self.batch(self.other, "Beach trip")
        body = self.search(q="beach").json()
        self.assertEqual((body["images"], body["groups"], body["jobs"]), ([], [], []))

    def test_merged_and_archived_groups_are_not_offered(self):
        self.group(self.user, "Old Alice", state=IdentityGroup.State.MERGED)
        self.group(self.user, "Archived Alice", state=IdentityGroup.State.ARCHIVED)
        self.group(self.user, "Alice", state=IdentityGroup.State.ACTIVE)
        self.assertEqual([g["display_name"] for g in self.search(q="alice").json()["groups"]], ["Alice"])

    def test_a_query_shorter_than_two_characters_is_a_plain_400(self):
        for params in ({}, {"q": ""}, {"q": "a"}, {"q": "  a "}):
            response = self.search(**params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn("q", response.json())

    def test_limit_defaults_to_five_and_is_capped_at_ten_per_kind(self):
        for index in range(12):
            self.image(self.user, f"sun-{index}.jpg")
            self.group(self.user, f"Sun {index}")
            self.batch(self.user, f"Sun trip {index}")
        default = self.search(q="sun").json()
        self.assertEqual([len(default[k]) for k in ("images", "groups", "jobs")], [5, 5, 5])
        capped = self.search(q="sun", limit=50).json()
        self.assertEqual([len(capped[k]) for k in ("images", "groups", "jobs")], [10, 10, 10])
        two = self.search(q="sun", limit=2).json()
        self.assertEqual(len(two["images"]), 2)

    def test_a_bad_limit_is_a_400(self):
        for bad in ("0", "-1", "abc"):
            self.assertEqual(self.search(q="sun", limit=bad).status_code, 400, bad)

    def test_nothing_found_is_empty_lists(self):
        body = self.search(q="zzzz").json()
        self.assertEqual((body["images"], body["groups"], body["jobs"]), ([], [], []))

    def test_rejected_images_have_no_thumbnail_and_report_rejected(self):
        self.image(self.user, "blurry.jpg", status="rejected")
        item = self.search(q="blurry").json()["images"][0]
        self.assertIsNone(item["thumbnail_url"])
        self.assertEqual(item["status"], "rejected")

    def test_the_search_is_rate_limited(self):
        rates = {**ScopedRateThrottle.THROTTLE_RATES, "search": "2/hour"}
        with patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            self.assertEqual(self.search(q="sun").status_code, 200)
            self.assertEqual(self.search(q="sun").status_code, 200)
            third = self.search(q="sun")
        self.assertEqual(third.status_code, 429)

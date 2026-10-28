"""Favourites: mark and unmark one photograph or many, filter the gallery by them, and never touch anyone else's."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import ImageUpload, UploadBatch

User = get_user_model()


class FavoriteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="fav-owner", email="fav-owner@example.com", password="Password!123", is_email_verified=True)
        self.stranger = User.objects.create_user(username="fav-other", email="fav-other@example.com", password="Password!123", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.photos = [ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name=f"fav-{i}.jpg", status="uploaded") for i in range(4)]
        self.refused = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name="refused.jpg", status="rejected")
        self.theirs = ImageUpload.objects.create(owner=self.stranger, batch=UploadBatch.objects.create(owner=self.stranger, upload_type="single"), original_name="theirs.jpg", status="uploaded")
        self.client = self.api(self.owner)

    @staticmethod
    def api(user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def url(self, photo):
        return reverse("image-favorite", kwargs={"pk": photo.pk})

    def test_a_photograph_can_be_marked_and_unmarked_and_both_are_idempotent(self):
        photo = self.photos[0]
        first = self.client.put(self.url(photo))
        self.assertEqual(first.status_code, 200, first.content)
        body = first.json()
        self.assertEqual((body["id"], body["is_favorite"]), (photo.pk, True))
        self.assertIsNotNone(body["favorited_at"])
        again = self.client.put(self.url(photo)).json()
        self.assertEqual(again["favorited_at"], body["favorited_at"])                       # marking again does not move the time
        removed = self.client.delete(self.url(photo)).json()
        self.assertEqual((removed["is_favorite"], removed["favorited_at"]), (False, None))
        self.assertEqual(self.client.delete(self.url(photo)).json()["is_favorite"], False)

    def test_marking_does_not_move_updated_at(self):
        photo = self.photos[0]
        before = ImageUpload.objects.get(pk=photo.pk).updated_at
        self.client.put(self.url(photo))
        self.assertEqual(ImageUpload.objects.get(pk=photo.pk).updated_at, before)

    def test_only_your_own_stored_photographs(self):
        for photo in (self.theirs, self.refused):
            with self.subTest(photo=photo.original_name):
                self.assertEqual(self.client.put(self.url(photo)).status_code, 404)
                self.assertEqual(self.client.delete(self.url(photo)).status_code, 404)
        self.assertFalse(ImageUpload.objects.get(pk=self.theirs.pk).is_favorite)
        self.assertEqual(APIClient().put(self.url(self.photos[0])).status_code, 401)

    def test_bulk_marks_many_and_reports_what_it_could_not(self):
        ids = [self.photos[0].pk, self.photos[1].pk, self.theirs.pk, self.refused.pk, 999999, self.photos[0].pk]
        response = self.client.post(reverse("image-bulk-favorite"), {"image_ids": ids, "favorite": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["updated_ids"], [self.photos[0].pk, self.photos[1].pk])
        self.assertEqual(body["not_found_ids"], [self.theirs.pk, self.refused.pk, 999999])
        self.assertEqual(set(ImageUpload.objects.filter(is_favorite=True).values_list("pk", flat=True)), {self.photos[0].pk, self.photos[1].pk})
        off = self.client.post(reverse("image-bulk-favorite"), {"image_ids": [self.photos[0].pk], "favorite": False}, format="json").json()
        self.assertEqual((off["favorite"], off["updated_ids"]), (False, [self.photos[0].pk]))
        self.assertEqual(set(ImageUpload.objects.filter(is_favorite=True).values_list("pk", flat=True)), {self.photos[1].pk})

    def test_bulk_validation(self):
        url = reverse("image-bulk-favorite")
        for body in ({}, {"image_ids": [], "favorite": True}, {"image_ids": [1]}, {"image_ids": ["x"], "favorite": True}, {"image_ids": list(range(1, 202)), "favorite": True}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, body, format="json").status_code, 400)

    def test_the_gallery_filter_and_the_row_field(self):
        self.client.post(reverse("image-bulk-favorite"), {"image_ids": [self.photos[1].pk, self.photos[2].pk], "favorite": True}, format="json")
        listing = lambda **params: {row["id"] for row in self.client.get(reverse("my-image-list"), params).json()["results"]}   # noqa: E731
        self.assertEqual(listing(favorite="true"), {self.photos[1].pk, self.photos[2].pk})
        self.assertEqual(listing(favorite="false"), {self.photos[0].pk, self.photos[3].pk, self.refused.pk})
        self.assertEqual(len(listing()), 5)
        self.assertEqual(self.client.get(reverse("my-image-list"), {"favorite": "maybe"}).status_code, 400)
        rows = {row["id"]: row for row in self.client.get(reverse("my-image-list")).json()["results"]}
        self.assertEqual([rows[p.pk]["is_favorite"] for p in self.photos], [False, True, True, False])
        detail = self.client.get(reverse("image-upload-detail", kwargs={"pk": self.photos[1].pk})).json()
        self.assertTrue(detail["is_favorite"])

    def test_the_filter_combines_with_the_others(self):
        self.client.put(self.url(self.photos[0]))
        other_batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        odd = ImageUpload.objects.create(owner=self.owner, batch=other_batch, original_name="odd.jpg", status="uploaded", is_favorite=True)
        rows = self.client.get(reverse("my-image-list"), {"favorite": "true", "batch": other_batch.pk}).json()["results"]
        self.assertEqual([row["id"] for row in rows], [odd.pk])

    def test_deleting_a_favourite_removes_it_with_the_photograph(self):
        self.client.put(self.url(self.photos[3]))
        self.assertEqual(self.client.delete(reverse("image-upload-detail", kwargs={"pk": self.photos[3].pk})).status_code, 204)
        self.assertEqual(self.client.get(reverse("my-image-list"), {"favorite": "true"}).json()["results"], [])

    def test_the_batch_responses_carry_the_field_too(self):
        self.client.put(self.url(self.photos[0]))
        body = self.client.get(reverse("upload-batch-detail", kwargs={"pk": self.batch.pk})).json()
        flags = {row["id"]: row["is_favorite"] for row in body["images"]}
        self.assertTrue(flags[self.photos[0].pk])
        self.assertFalse(flags[self.photos[1].pk])

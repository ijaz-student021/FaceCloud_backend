"""GET /api/images/ and GET /api/images/my/ can be narrowed to one upload batch."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import ImageUpload, UploadBatch

User = get_user_model()


class BatchFilterTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bf", email="bf@example.com", password="OrchidRiver!4729", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="bf-other", email="bf-other@example.com", password="OrchidRiver!4729", is_email_verified=True,
        )
        self.client.force_authenticate(self.user)
        self.first = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        self.second = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        self.theirs = UploadBatch.objects.create(owner=self.other, upload_type="multiple")
        for batch, names in ((self.first, ("a1.jpg", "a2.jpg")), (self.second, ("b1.jpg",)), (self.theirs, ("c1.jpg",))):
            for name in names:
                ImageUpload.objects.create(owner=batch.owner, batch=batch, original_name=name, status="uploaded")

    def names(self, route, **params):
        body = self.client.get(reverse(route), params).json()
        rows = body["results"] if isinstance(body, dict) else body
        return sorted(row["original_name"] for row in rows)

    def test_both_lists_can_be_narrowed_to_one_batch(self):
        for route in ("image-upload-list", "my-image-list"):
            self.assertEqual(self.names(route, batch=self.first.pk), ["a1.jpg", "a2.jpg"], route)
            self.assertEqual(self.names(route, batch=self.second.pk), ["b1.jpg"], route)

    def test_without_the_filter_every_own_photo_is_listed_and_none_of_anyone_elses(self):
        for route in ("image-upload-list", "my-image-list"):
            self.assertEqual(self.names(route), ["a1.jpg", "a2.jpg", "b1.jpg"], route)

    def test_someone_elses_batch_id_gives_an_empty_list(self):
        for route in ("image-upload-list", "my-image-list"):
            self.assertEqual(self.names(route, batch=self.theirs.pk), [], route)
            self.assertEqual(self.names(route, batch=999999), [], route)

    def test_a_bad_batch_value_is_a_400(self):
        for route in ("image-upload-list", "my-image-list"):
            for bad in ("abc", "-1", "1.5"):
                response = self.client.get(reverse(route), {"batch": bad})
                self.assertEqual(response.status_code, 400, (route, bad))
                self.assertIn("batch", response.json())

    def test_the_filter_combines_with_the_my_images_search(self):
        self.assertEqual(self.names("my-image-list", batch=self.first.pk, search="a2"), ["a2.jpg"])

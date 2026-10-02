"""One image's own page must say what the gallery says about it.

The detail view used to answer with the plain upload serializer while the
gallery used the richer one, so a photo's own page could not report whether it
had been accepted -- the single thing the person who uploaded it most wants to
know. These tests pin the two shapes together so they cannot drift apart again.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from quality_control.models import ImageQualityDecision

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .tests import make_image

User = get_user_model()


class ImageDetailMatchesListTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="detail-owner", email="detail-owner@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        token = RefreshToken.for_user(self.owner)
        token["role"] = self.owner.role
        token["token_version"] = self.owner.token_version
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        batch = UploadBatch.objects.create(
            owner=self.owner, upload_type="single", status="uploaded", display_name="July set",
        )
        self.image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="portrait.jpg",
            file_extension=".jpg", mime_type="image/jpeg", status="uploaded",
        )
        self.image.original_file.save("portrait.jpg", make_image(), save=True)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.owner, image=self.image, status="completed",
        )
        ImageQualityDecision.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=preprocessing,
            status="completed", decision=ImageQualityDecision.Decision.REJECTED,
            overall_score=0.21, rejection_reasons=["Too blurred to use"],
        )

    def detail(self):
        return self.client.get(reverse("image-upload-detail", args=[self.image.pk]))

    def listed(self):
        response = self.client.get(reverse("my-image-list"))
        return next(row for row in response.data["results"] if row["id"] == self.image.pk)

    def test_the_detail_view_reports_the_same_fields_as_the_gallery(self):
        detail = self.detail()

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(set(self.listed()), set(detail.data))

    def test_the_detail_view_says_whether_the_photo_was_accepted(self):
        detail = self.detail()

        self.assertIn("display_status", detail.data)
        self.assertEqual(detail.data["display_status"], self.listed()["display_status"])

    def test_the_detail_view_carries_the_quality_decision_and_its_reasons(self):
        detail = self.detail()

        self.assertIsNotNone(detail.data["quality_decision"])
        self.assertEqual(detail.data["quality_decision"]["decision"], "rejected")
        self.assertEqual(detail.data["quality_decision"]["rejection_reasons"], ["Too blurred to use"])

    def test_the_detail_view_names_the_upload_the_photo_came_from(self):
        detail = self.detail()

        self.assertEqual(detail.data["batch_display_name"], "July set")
        self.assertEqual(detail.data["upload_type"], "single")

    def test_the_detail_view_still_carries_the_download_and_thumbnail_links(self):
        detail = self.detail()

        self.assertTrue(detail.data["download_url"])
        self.assertTrue(detail.data["thumbnail_url"])

    def test_somebody_elses_photo_is_still_not_found(self):
        other = User.objects.create_user(
            username="detail-other", email="detail-other@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        token = RefreshToken.for_user(other)
        token["role"] = other.role
        token["token_version"] = other.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        response = client.get(reverse("image-upload-detail", args=[self.image.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_anonymous_request_is_refused(self):
        response = APIClient().get(reverse("image-upload-detail", args=[self.image.pk]))

        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_the_owner_can_still_delete_their_photo(self):
        response = self.client.delete(reverse("image-upload-detail", args=[self.image.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ImageUpload.objects.filter(pk=self.image.pk).exists())

    def test_reading_one_photo_does_not_cost_a_query_per_related_record(self):
        # The detail view now carries enhancement, identity and quality data, so
        # the point of sharing the gallery's queryset is that it still loads in
        # a fixed number of queries rather than one per relation: the user, one
        # joined row, and one prefetch each for enhancements and identity faces.
        with self.assertNumQueries(4):
            self.client.get(reverse("image-upload-detail", args=[self.image.pk]))

"""T-009: the photo behind a review.

A moderator cannot judge a photo they cannot see, so this endpoint exists --
but these tests are mostly about what it does *not* give away. It is keyed by
review rather than by image, serves thumbnails rather than originals, and is
gated by the same organization scope and role as the review itself.
"""
import io

import numpy as np
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image
from rest_framework import status

from organizations.models import OrganizationMembership
from organizations.services import create_organization
from thumbnails.models import ImageThumbnail
from uploads.models import ImageUpload, UploadBatch

from .tests import ModerationFixtureMixin


def photo_bytes(width=400, height=300, seed=7):
    pixels = np.random.default_rng(seed).integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class ReviewImageMixin(ModerationFixtureMixin):
    def make_stored_image(self, owner, organization, name="portrait.jpg"):
        """An ImageUpload with a real file behind it, so a thumbnail can be made."""
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name=name, file_extension=".jpg",
            mime_type="image/jpeg", status="uploaded",
        )
        image.original_file.save(name, ContentFile(photo_bytes()), save=True)
        return image

    def image_url(self, review, organization=None, **params):
        url = reverse("moderation:review-image", kwargs={
            "organization_id": (organization or self.organization).pk, "review_id": review.pk,
        })
        if params:
            url += "?" + "&".join(f"{key}={value}" for key, value in params.items())
        return url


class ModeratorReviewImageTests(ReviewImageMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("image-owner")
        self.organization = create_organization(name="Image Co", owner=self.owner)
        self.moderator = self.make_user("image-moderator")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator,
            role=OrganizationMembership.Role.MODERATOR,
        )
        image = self.make_stored_image(self.owner, self.organization)
        self.review, self.decision = self.make_quality_review(self.owner, self.organization, image=image)

    def read(self, response):
        return b"".join(response.streaming_content)

    def test_a_moderator_can_see_the_photo_behind_a_review(self):
        response = self.authenticated_client(self.moderator).get(self.image_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertGreater(len(self.read(response)), 0)

    def test_what_comes_back_is_a_real_decodable_image(self):
        response = self.authenticated_client(self.moderator).get(self.image_url(self.review))

        with Image.open(io.BytesIO(self.read(response))) as served:
            self.assertEqual(served.format, "JPEG")
            self.assertLessEqual(max(served.size), 256)

    def test_the_organization_owner_can_see_it_too(self):
        response = self.authenticated_client(self.owner).get(self.image_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_plain_member_cannot_see_it(self):
        member = self.make_user("image-plain-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )

        response = self.authenticated_client(member).get(self.image_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_someone_outside_the_organization_gets_a_non_disclosing_404(self):
        response = self.authenticated_client(self.make_user("image-outsider")).get(
            self.image_url(self.review),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_anonymous_request_is_refused(self):
        from rest_framework.test import APIClient

        response = APIClient().get(self.image_url(self.review))

        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_a_review_in_another_organization_is_not_reachable(self):
        other_owner = self.make_user("image-other-owner")
        other_org = create_organization(name="Other Image Co", owner=other_owner)
        other_image = self.make_stored_image(other_owner, other_org)
        other_review, _ = self.make_quality_review(other_owner, other_org, image=other_image)

        response = self.authenticated_client(self.moderator).get(
            self.image_url(other_review),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_moderator_who_loses_the_role_loses_the_photo_with_it(self):
        membership = OrganizationMembership.objects.get(
            organization=self.organization, user=self.moderator,
        )
        membership.role = OrganizationMembership.Role.MEMBER
        membership.save(update_fields=["role"])

        response = self.authenticated_client(self.moderator).get(self.image_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_suspended_moderator_loses_the_photo_too(self):
        membership = OrganizationMembership.objects.get(
            organization=self.organization, user=self.moderator,
        )
        membership.status = OrganizationMembership.Status.SUSPENDED
        membership.save(update_fields=["status"])

        response = self.authenticated_client(self.moderator).get(self.image_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_larger_configured_size_can_be_asked_for(self):
        response = self.authenticated_client(self.moderator).get(
            self.image_url(self.review, size=1024),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with Image.open(io.BytesIO(self.read(response))) as served:
            # The source is 400x300, and a thumbnail never enlarges a photo.
            self.assertEqual(served.size, (400, 300))

    def test_an_unconfigured_size_is_refused(self):
        response = self.authenticated_client(self.moderator).get(
            self.image_url(self.review, size=37),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_size_that_is_not_a_number_is_refused(self):
        response = self.authenticated_client(self.moderator).get(
            self.image_url(self.review, size="huge"),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_review_whose_photo_is_missing_answers_404_rather_than_failing(self):
        review, _ = self.make_quality_review(self.owner, self.organization)

        response = self.authenticated_client(self.moderator).get(self.image_url(review))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_review_that_does_not_exist_answers_404(self):
        url = reverse("moderation:review-image", kwargs={
            "organization_id": self.organization.pk, "review_id": 9_999_999,
        })

        response = self.authenticated_client(self.moderator).get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_thumbnail_is_made_once_and_reused(self):
        client = self.authenticated_client(self.moderator)
        client.get(self.image_url(self.review))
        client.get(self.image_url(self.review))

        self.assertEqual(ImageThumbnail.objects.filter(image=self.review.image, size=256).count(), 1)

    def test_the_browser_is_told_the_photo_is_private(self):
        response = self.authenticated_client(self.moderator).get(self.image_url(self.review))

        self.assertIn("private", response["Cache-Control"])

    def test_a_decided_review_still_shows_its_photo(self):
        from .services import decide_review

        decide_review(review=self.review, actor=self.moderator, action="approve")

        response = self.authenticated_client(self.moderator).get(self.image_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ReviewFilenameTests(ReviewImageMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("filename-owner")
        self.organization = create_organization(name="Filename Co", owner=self.owner)
        image = self.make_stored_image(self.owner, self.organization, name="dark-portrait.jpg")
        self.review, _ = self.make_quality_review(self.owner, self.organization, image=image)

    def test_the_queue_carries_the_photos_own_name(self):
        response = self.authenticated_client(self.owner).get(
            reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk}),
        )

        rows = response.data["results"] if "results" in response.data else response.data
        self.assertEqual(rows[0]["image_name"], "dark-portrait.jpg")

    def test_the_detail_view_carries_it_too(self):
        response = self.authenticated_client(self.owner).get(reverse(
            "moderation:review-detail",
            kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk},
        ))

        self.assertEqual(response.data["image_name"], "dark-portrait.jpg")

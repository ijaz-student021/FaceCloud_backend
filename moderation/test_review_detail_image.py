"""The review detail carries the photograph's own facts, for the Image Review screen."""
from django.test import TestCase
from django.urls import reverse

from organizations.models import OrganizationMembership
from organizations.services import create_organization
from validation.models import FaceValidationResult

from .tests import ModerationFixtureMixin


class ReviewDetailImageTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("detail-owner")
        self.organization = create_organization(name="Detail Co", owner=self.owner)
        self.moderator = self.make_user("detail-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.member = self.make_user("detail-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member)
        self.review, self.decision = self.make_quality_review(self.member, self.organization)
        self.decision.rejection_reasons = ["borderline_blur"]
        self.decision.overall_score = 71.5
        self.decision.save()
        self.image = self.review.image
        self.image.width, self.image.height, self.image.file_size = 640, 480, 4321
        self.image.save()
        FaceValidationResult.objects.create(owner=self.member, image=self.image, preprocessing_result=self.decision.preprocessing_result,
                                            decision="accepted", face_count=2)
        self.client = self.authenticated_client(self.moderator)
        self.url = reverse("moderation:review-detail", kwargs={"organization_id": self.organization.pk, "review_id": self.review.pk})

    def test_the_detail_has_an_image_object_with_name_size_uploader_faces_quality_and_preview(self):
        body = self.client.get(self.url).json()
        image = body["image"]
        self.assertEqual((image["id"], image["name"], image["width"], image["height"], image["file_size"]), (self.image.pk, "face.jpg", 640, 480, 4321))
        self.assertEqual((image["uploader"]["username"], image["face_count"]), ("detail-member", 2))
        self.assertEqual((image["quality"]["decision"], image["quality"]["overall_score"], image["quality"]["rejection_reasons"]),
                         ("needs_review", 71.5, ["borderline_blur"]))
        self.assertEqual(image["quality"]["warnings"], [])
        self.assertTrue(image["preview_url"].endswith(f"/api/organizations/{self.organization.pk}/moderator/reviews/{self.review.pk}/image/"))

    def test_the_review_fields_the_queue_uses_are_unchanged(self):
        body = self.client.get(self.url).json()
        for key in ("id", "organization", "image_id", "image_name", "source", "status", "ai_recommendation", "ai_confidence", "is_claimed",
                    "due_at", "is_overdue", "decision_reason", "created_at"):
            self.assertIn(key, body)
        listed = self.client.get(reverse("moderation:review-list", kwargs={"organization_id": self.organization.pk})).json()
        self.assertNotIn("image", listed[0])                 # the queue list stays light

    def test_a_photo_with_no_checks_yet_reads_with_nulls_not_errors(self):
        from .models import ModeratorReview

        bare = ModeratorReview.objects.create(organization=self.organization, image=self.make_image(self.member, self.organization),
                                              source=ModeratorReview.Source.QUALITY, ai_recommendation="needs_review")
        body = self.client.get(reverse("moderation:review-detail", kwargs={"organization_id": self.organization.pk, "review_id": bare.pk})).json()
        self.assertEqual((body["image"]["face_count"], body["image"]["quality"]["decision"], body["image"]["quality"]["rejection_reasons"]), (None, None, []))

    def test_the_detail_costs_a_fixed_number_of_queries(self):
        with self.assertNumQueries(3):                         # the signed-in user, the membership, the review with its photo and checks
            self.client.get(self.url)

    def test_another_organizations_review_is_not_found(self):
        other = create_organization(name="Other Detail Co", owner=self.make_user("other-detail-owner"))
        url = reverse("moderation:review-detail", kwargs={"organization_id": other.pk, "review_id": self.review.pk})
        self.assertEqual(self.authenticated_client(other.memberships.first().user).get(url).status_code, 404)

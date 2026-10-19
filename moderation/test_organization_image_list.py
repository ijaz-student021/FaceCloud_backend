"""GET .../moderator/images/: the organization's photographs, filtered by outcome, for the Rejected Images screen."""
from django.test import TestCase
from django.urls import reverse

from organizations.models import OrganizationMembership
from organizations.services import create_organization, create_personal_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import ModeratorReview
from .tests import ModerationFixtureMixin


class OrganizationImageListTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("img-owner")
        self.organization = create_organization(name="Img Co", owner=self.owner)
        self.moderator = self.make_user("img-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.member = self.make_user("img-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member)
        self.batch = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="multiple")
        self.client = self.authenticated_client(self.moderator)
        self.url = reverse("moderation:image-list", kwargs={"organization_id": self.organization.pk})

    def photo(self, name, decision=None, status="uploaded", reasons=None, score=None, batch=None, **extra):
        image = ImageUpload.objects.create(owner=self.member, batch=batch or self.batch, original_name=name, status=status, file_size=100, **extra)
        if decision:
            pre = ImagePreprocessingResult.objects.create(owner=self.member, image=image, status="completed")
            ImageQualityDecision.objects.create(owner=self.member, image=image, preprocessing_result=pre, status="completed",
                                                decision=decision, rejection_reasons=reasons or [], overall_score=score)
        return image

    def listed(self, **query):
        response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return body["results"] if isinstance(body, dict) else body

    def test_the_rejected_filter_includes_photos_refused_at_upload_and_rejected_by_quality(self):
        self.photo("good.jpg", "accepted", score=90)
        self.photo("blurry.jpg", "rejected", reasons=["severe_blur"], score=20)
        self.photo("virus.exe", status="rejected", rejection_reason="Unsupported file type.")
        self.photo("maybe.jpg", "needs_review")
        rows = {row["original_name"]: row for row in self.listed(decision="rejected")}
        self.assertEqual(set(rows), {"blurry.jpg", "virus.exe"})
        self.assertEqual((rows["blurry.jpg"]["decision"], rows["blurry.jpg"]["rejection_reasons"], rows["blurry.jpg"]["overall_score"]), ("rejected", ["severe_blur"], 20.0))
        self.assertEqual((rows["virus.exe"]["rejection_reasons"], rows["virus.exe"]["preview_url"]), (["Unsupported file type."], None))
        self.assertTrue(rows["blurry.jpg"]["preview_url"].endswith(f"/moderator/images/{rows['blurry.jpg']['id']}/preview/"))

    def test_needs_review_and_accepted_filters_and_no_filter(self):
        self.photo("a.jpg", "accepted")
        self.photo("b.jpg", "needs_review")
        self.assertEqual([r["original_name"] for r in self.listed(decision="needs_review")], ["b.jpg"])
        self.assertEqual([r["original_name"] for r in self.listed(decision="accepted")], ["a.jpg"])
        self.assertEqual(len(self.listed()), 2)
        self.assertEqual(self.client.get(self.url, {"decision": "weird"}).status_code, 400)

    def test_each_row_names_the_uploader_and_the_review_when_there_is_one(self):
        image = self.photo("r.jpg", "needs_review")
        review = ModeratorReview.objects.create(organization=self.organization, image=image, source="quality", ai_recommendation="needs_review")
        row = self.listed()[0]
        self.assertEqual((row["uploader"]["username"], row["review_id"], row["batch_id"]), ("img-member", review.pk, self.batch.pk))
        for key in ("id", "created_at", "width", "height", "file_size"):
            self.assertIn(key, row)

    def test_search_and_batch_filters(self):
        other_batch = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="single")
        self.photo("sunset.jpg")
        self.photo("portrait.jpg", batch=other_batch)
        self.assertEqual([r["original_name"] for r in self.listed(q="sun")], ["sunset.jpg"])
        self.assertEqual([r["original_name"] for r in self.listed(q="img-member")][0:1], ["portrait.jpg"])      # by uploader, newest first
        self.assertEqual([r["original_name"] for r in self.listed(batch=other_batch.pk)], ["portrait.jpg"])
        self.assertEqual(self.client.get(self.url, {"batch": "x"}).status_code, 400)

    def test_paging(self):
        for index in range(5):
            self.photo(f"p{index}.jpg")
        page = self.client.get(self.url, {"page": 1, "page_size": 2}).json()
        self.assertEqual((page["count"], len(page["results"])), (5, 2))

    def test_a_list_costs_a_fixed_number_of_queries(self):
        for index in range(6):
            self.photo(f"q{index}.jpg", "needs_review")
        with self.assertNumQueries(4):                         # user, membership, the photos, their reviews
            self.listed()

    def test_other_organizations_and_personal_workspaces_never_appear(self):
        other = create_organization(name="Other Img Co", owner=self.make_user("other-img-owner"))
        UploadBatch.objects.create(owner=self.member, organization=other, upload_type="single")
        ImageUpload.objects.create(owner=self.member, batch=UploadBatch.objects.get(organization=other), original_name="theirs.jpg", status="uploaded")
        personal = create_personal_organization(self.member)
        personal_batch = UploadBatch.objects.create(owner=self.member, organization=personal, upload_type="single")
        self.photo("mine-private.jpg", batch=personal_batch)
        self.photo("shared.jpg")
        self.assertEqual([r["original_name"] for r in self.listed()], ["shared.jpg"])

    def test_a_plain_member_is_refused_and_an_outsider_learns_nothing(self):
        self.assertEqual(self.authenticated_client(self.member).get(self.url).status_code, 403)
        self.assertEqual(self.authenticated_client(self.make_user("img-outsider")).get(self.url).status_code, 404)

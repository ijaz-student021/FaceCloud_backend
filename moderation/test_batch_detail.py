"""Organization batch detail with per-image outcomes (B04)."""
from django.test import TestCase
from django.urls import reverse

from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import BatchImageProgress, ImageUpload, UploadBatch

from .tests import ModerationFixtureMixin


class ModeratorBatchDetailTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("batch-owner")
        self.organization = create_organization(name="Batch Co", owner=self.owner)
        self.moderator = self.make_user("batch-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.member = self.make_user("batch-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member)
        self.batch = UploadBatch.objects.create(owner=self.member, organization=self.organization,
                                                upload_type="multiple", total_files=3, accepted_files=2,
                                                rejected_files=1, processing_status="partial")
        statuses = [("uploaded", "completed"), ("uploaded", "failed"), ("rejected", None)]
        for index, (upload_status, progress_status) in enumerate(statuses):
            image = ImageUpload.objects.create(owner=self.member, batch=self.batch, original_name=f"{index}.jpg",
                                               status=upload_status,
                                               rejection_reason="Unsupported file type." if upload_status == "rejected" else "")
            if progress_status:
                BatchImageProgress.objects.update_or_create(
                    image=image, defaults={"owner": self.member, "batch": self.batch, "status": progress_status,
                                           "failure_reason": "Preprocessing failed." if progress_status == "failed" else ""})
        self.url = reverse("moderation:batch-detail",
                           kwargs={"organization_id": self.organization.pk, "batch_id": self.batch.pk})

    def test_staff_see_every_image_outcome(self):
        for user in (self.owner, self.moderator):
            with self.subTest(user=user.username):
                body = self.authenticated_client(user).get(self.url).json()
                self.assertEqual(body["batch"]["id"], self.batch.pk)
                self.assertEqual([row["original_name"] for row in body["images"]], ["0.jpg", "1.jpg", "2.jpg"])
                failed = body["images"][1]
                self.assertEqual((failed["progress_status"], failed["failure_reason"]), ("failed", "Preprocessing failed."))
                self.assertEqual(body["images"][2]["rejection_reason"], "Unsupported file type.")

    def test_the_rows_reconcile_with_the_batch_counters(self):
        body = self.authenticated_client(self.owner).get(self.url).json()
        self.assertEqual(len(body["images"]), body["batch"]["total_files"])
        rejected = sum(row["upload_status"] == "rejected" for row in body["images"])
        self.assertEqual(rejected, body["batch"]["rejected_files"])
        self.assertEqual(sum(body["counts_from_images"].values()), len(body["images"]))

    def test_a_plain_member_is_refused_and_an_outsider_learns_nothing(self):
        self.assertEqual(self.authenticated_client(self.member).get(self.url).status_code, 403)
        outsider = self.make_user("batch-outsider")
        self.assertEqual(self.authenticated_client(outsider).get(self.url).status_code, 404)

    def test_another_organizations_batch_is_not_found_here(self):
        other_owner = self.make_user("other-owner")
        other_org = create_organization(name="Other", owner=other_owner)
        foreign = UploadBatch.objects.create(owner=other_owner, organization=other_org, upload_type="single")
        url = reverse("moderation:batch-detail",
                      kwargs={"organization_id": self.organization.pk, "batch_id": foreign.pk})
        self.assertEqual(self.authenticated_client(self.owner).get(url).status_code, 404)

    def test_no_image_content_is_exposed(self):
        text = str(self.authenticated_client(self.owner).get(self.url).json())
        for leak in ("download_url", "original_file", "thumbnail", "embedding"):
            self.assertNotIn(leak, text)

"""The Batches screen: a name, a progress figure, a count per outcome, and per photo a size, an outcome and a preview."""
from django.test import TestCase
from django.urls import reverse

from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import BatchImageProgress, ImageUpload, UploadBatch

from .tests import ModerationFixtureMixin


class BatchScreenFieldsTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("screen-owner")
        self.organization = create_organization(name="Screen Co", owner=self.owner)
        self.moderator = self.make_user("screen-mod")
        OrganizationMembership.objects.create(organization=self.organization, user=self.moderator,
                                              role=OrganizationMembership.Role.MODERATOR)
        self.member = self.make_user("screen-member")
        OrganizationMembership.objects.create(organization=self.organization, user=self.member)
        self.client = self.authenticated_client(self.moderator)

    def batch(self, outcomes, **fields):
        batch = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type=fields.pop("upload_type", "multiple"),
                                           total_files=len(outcomes), accepted_files=len(outcomes), **fields)
        for index, (outcome, percent, size) in enumerate(outcomes):
            image = ImageUpload.objects.create(
                owner=self.member, batch=batch, original_name=f"{index}.jpg", file_size=size,
                status="rejected" if outcome == "rejected" and index == 0 and fields.get("refused") else "uploaded")
            BatchImageProgress.objects.update_or_create(
                image=image, defaults={"owner": self.member, "batch": batch, "status": outcome, "progress_percentage": percent})
        return batch

    def listed(self):
        response = self.client.get(reverse("moderation:batch-list", kwargs={"organization_id": self.organization.pk}))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_a_row_has_a_name_a_progress_figure_and_a_count_per_outcome(self):
        self.batch([("accepted", 100, 10), ("accepted", 100, 20), ("rejected", 100, 30), ("needs_review", 100, 40), ("failed", 50, 50)],
                   processing_status="partial", original_archive_name="staff_portraits.zip", upload_type="zip")
        row = self.listed()[0]
        self.assertEqual(row["name"], "staff_portraits.zip")
        self.assertEqual(row["counts"], {"accepted": 2, "rejected": 1, "needs_review": 1, "failed": 1})
        self.assertEqual(row["progress_percentage"], 90.0)
        for old in ("id", "owner_username", "upload_type", "processing_status", "total_files", "accepted_files", "created_at"):
            self.assertIn(old, row)

    def test_the_name_prefers_the_label_then_the_zip_name_then_the_number(self):
        labelled = self.batch([("accepted", 100, 1)], display_name="Q3 team shots", original_archive_name="q3.zip", upload_type="zip")
        zipped = self.batch([("accepted", 100, 1)], original_archive_name="q4.zip", upload_type="zip")
        plain = self.batch([("accepted", 100, 1)])
        names = {row["id"]: row["name"] for row in self.listed()}
        self.assertEqual((names[labelled.pk], names[zipped.pk], names[plain.pk]), ("Q3 team shots", "q4.zip", f"Upload {plain.pk}"))

    def test_a_batch_without_progress_rows_reads_by_its_status_and_counts_zero(self):
        queued = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="single", processing_status="queued")
        done = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="single", processing_status="completed")
        rows = {row["id"]: row for row in self.listed()}
        self.assertEqual((rows[queued.pk]["progress_percentage"], rows[done.pk]["progress_percentage"]), (0.0, 100.0))
        self.assertEqual(rows[queued.pk]["counts"], {"accepted": 0, "rejected": 0, "needs_review": 0, "failed": 0})

    def test_a_list_of_batches_costs_a_fixed_number_of_queries(self):
        for _ in range(6):
            self.batch([("accepted", 100, 1), ("rejected", 100, 1)])
        with self.assertNumQueries(3):                      # the signed-in user, the membership, and one query for every batch with its counts
            self.listed()

    def test_detail_images_have_a_size_an_outcome_and_a_preview(self):
        batch = self.batch([("accepted", 100, 1200), ("needs_review", 100, 3400), ("failed", 20, 56)], processing_status="partial")
        body = self.client.get(reverse("moderation:batch-detail", kwargs={"organization_id": self.organization.pk, "batch_id": batch.pk})).json()
        self.assertEqual([(row["file_size"], row["outcome"]) for row in body["images"]],
                         [(1200, "accepted"), (3400, "needs_review"), (56, "failed")])
        first = body["images"][0]
        self.assertTrue(first["preview_url"].endswith(f"/api/organizations/{self.organization.pk}/moderator/images/{first['id']}/preview/"))
        self.assertEqual(body["batch"]["counts"], {"accepted": 1, "rejected": 0, "needs_review": 1, "failed": 1})
        self.assertIn("name", body["batch"])

    def test_a_photo_refused_at_upload_has_no_preview(self):
        batch = UploadBatch.objects.create(owner=self.member, organization=self.organization, upload_type="multiple")
        refused = ImageUpload.objects.create(owner=self.member, batch=batch, original_name="x.exe", status="rejected", file_size=9)
        BatchImageProgress.objects.update_or_create(image=refused, defaults={"owner": self.member, "batch": batch, "status": "rejected"})
        body = self.client.get(reverse("moderation:batch-detail", kwargs={"organization_id": self.organization.pk, "batch_id": batch.pk})).json()
        self.assertEqual((body["images"][0]["outcome"], body["images"][0]["preview_url"]), ("rejected", None))

    def test_another_organizations_batches_are_not_in_the_list(self):
        other = create_organization(name="Other Co", owner=self.make_user("other-screen-owner"))
        UploadBatch.objects.create(owner=self.member, organization=other, upload_type="single")
        self.assertEqual(self.listed(), [])

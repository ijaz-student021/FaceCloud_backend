"""T-007 Phase E: batch-management moderator actions (retry + cancel).

"Reprocess at a different enhancement level" is explicitly out of scope --
it does not exist anywhere in the codebase today, not even an owner-facing
version, and is real new design work rather than a reuse. Only retry
(reuses uploads.services.retry_batch unchanged) and cancel (uploads.services
.cancel_batch, newly extracted from the owner-facing view so this and the
owner endpoint share one implementation) are covered here.
"""
from unittest.mock import patch

from django.urls import reverse
from django.test import TestCase
from rest_framework import status

from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from uploads.models import BatchImageProgress, ImagePreprocessingResult, ImageUpload, UploadBatch

from .tests import ModerationFixtureMixin


class ModeratorBatchManagementTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("batch-owner")
        self.organization = create_organization(name="Batch Co", owner=self.owner)
        self.other_owner = self.make_user("other-batch-owner")
        self.other_org = create_organization(name="Other Batch Co", owner=self.other_owner)
        self.owner_client = self.authenticated_client(self.owner)

    def make_batch(self, owner, organization, processing_status="uploaded"):
        return UploadBatch.objects.create(
            owner=owner, upload_type="single", organization=organization,
            processing_status=processing_status,
        )

    def list_url(self, organization=None):
        return reverse(
            "moderation:batch-list", kwargs={"organization_id": (organization or self.organization).pk},
        )

    def retry_url(self, batch, organization=None):
        return reverse(
            "moderation:batch-retry",
            kwargs={"organization_id": (organization or self.organization).pk, "batch_id": batch.pk},
        )

    def cancel_url(self, batch, organization=None):
        return reverse(
            "moderation:batch-cancel",
            kwargs={"organization_id": (organization or self.organization).pk, "batch_id": batch.pk},
        )

    def test_the_batch_list_only_shows_this_organizations_batches(self):
        mine = self.make_batch(self.owner, self.organization)
        self.make_batch(self.other_owner, self.other_org)

        response = self.owner_client.get(self.list_url())

        ids = [row["id"] for row in response.data["results"]] if "results" in response.data else [
            row["id"] for row in response.data
        ]
        self.assertEqual(ids, [mine.pk])

    def test_the_batch_list_can_filter_by_processing_status(self):
        self.make_batch(self.owner, self.organization, processing_status="completed")
        failed = self.make_batch(self.owner, self.organization, processing_status="failed")

        response = self.owner_client.get(self.list_url(), {"processing_status": "failed"})

        ids = [row["id"] for row in response.data["results"]] if "results" in response.data else [
            row["id"] for row in response.data
        ]
        self.assertEqual(ids, [failed.pk])

    def test_retrying_a_batch_with_no_failed_images_is_a_400(self):
        batch = self.make_batch(self.owner, self.organization, processing_status="failed")

        response = self.owner_client.post(self.retry_url(batch))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("uploads.services.enqueue_batch_preprocessing")
    def test_retrying_a_batch_with_failed_images_requeues_them_and_audits(self, enqueue):
        """Mirrors operations/test_admin_management.py's AdminBatchRetryEndpointTests
        fixture and assertions exactly, since this reuses the identical
        uploads.services.retry_batch() the admin endpoint already calls.
        """
        batch = self.make_batch(self.owner, self.organization, processing_status="failed")
        image = ImageUpload.objects.create(
            owner=self.owner, batch=batch, original_name="face.jpg", status="uploaded",
        )
        # ImageUpload's post_save signal already created the BatchImageProgress
        # row (uploads/signals.py) -- just move it into the failed state.
        ImagePreprocessingResult.objects.create(
            owner=self.owner, image=image, status="failed", failure_reason="decoder failed",
        )
        BatchImageProgress.objects.filter(image=image).update(
            status="failed", failure_stage="preprocessing", failure_reason="decoder failed",
        )

        response = self.owner_client.post(self.retry_url(batch))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        batch.refresh_from_db()
        self.assertEqual(batch.retry_count, 1)
        enqueue.assert_called_once()
        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=self.organization,
                action=OrganizationAuditEvent.Action.BATCH_RETRIED_BY_MODERATOR,
                target_id=batch.pk,
            ).exists()
        )

    def test_cancelling_a_batch_marks_it_cancelled_and_audits(self):
        batch = self.make_batch(self.owner, self.organization, processing_status="queued")

        response = self.owner_client.post(self.cancel_url(batch))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        batch.refresh_from_db()
        self.assertEqual(batch.processing_status, "cancelled")
        self.assertTrue(batch.cancellation_requested)
        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=self.organization,
                action=OrganizationAuditEvent.Action.BATCH_CANCELLED_BY_MODERATOR,
                target_id=batch.pk,
            ).exists()
        )

    def test_a_batch_from_another_organization_is_not_visible_or_actionable(self):
        other_batch = self.make_batch(self.other_owner, self.other_org, processing_status="failed")

        retry_response = self.owner_client.post(self.retry_url(other_batch))
        cancel_response = self.owner_client.post(self.cancel_url(other_batch))

        self.assertEqual(retry_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(cancel_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_plain_member_cannot_retry_or_cancel_a_batch(self):
        member = self.make_user("batch-member")
        OrganizationMembership.objects.create(
            organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER,
        )
        batch = self.make_batch(self.owner, self.organization, processing_status="failed")

        response = self.authenticated_client(member).post(self.retry_url(batch))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_owner_facing_cancel_endpoint_still_works_unchanged(self):
        """Guards the uploads.services.cancel_batch extraction: same behavior as before."""
        batch = self.make_batch(self.owner, self.organization, processing_status="queued")

        response = self.owner_client.post(
            reverse("upload-batch-cancel", kwargs={"batch_id": batch.pk}),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        batch.refresh_from_db()
        self.assertEqual(batch.processing_status, "cancelled")

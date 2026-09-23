"""T-006: organization-wide data export.

Deliberately separate from privacy/tests.py (which covers the personal
export) -- this is the org-scoped counterpart, following the same
list/detail/download-view pattern as organizations/ and moderation/.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from identities.models import IdentityGroup
from moderation.models import ModeratorReview
from organizations.models import OrganizationAuditEvent, OrganizationMembership
from organizations.services import create_organization
from quality_control.models import ImageQualityDecision
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import OrganizationExport
from .services import (
    build_organization_export_payload,
    enqueue_organization_export,
    expire_due_organization_exports,
    process_organization_export,
)

User = get_user_model()


class ExportFixtureMixin:
    def make_user(self, name, **extra):
        defaults = {"is_email_verified": True, "email": f"{name}@example.com"}
        defaults.update(extra)
        return User.objects.create_user(username=name, password="ForestRiver!4821", **defaults)

    def authenticated_client(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def build_org_content(self, owner, organization, name="a.jpg"):
        """One batch/image/quality-decision/identity-group/review, all org-tagged."""
        batch = UploadBatch.objects.create(owner=owner, upload_type="single", organization=organization)
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=name, status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        quality = ImageQualityDecision.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, status="completed",
            decision=ImageQualityDecision.Decision.NEEDS_REVIEW, overall_score=40.0,
        )
        group = IdentityGroup.objects.create(
            owner=owner, organization=organization, display_name="Person 001",
            embedding_dimension=3, provider="test", policy_version="test-v1",
            protected_centroid=b"private-centroid",
        )
        review = ModeratorReview.objects.create(
            organization=organization, image=image, source=ModeratorReview.Source.QUALITY,
            ai_recommendation="needs_review", quality_decision=quality,
        )
        return {"batch": batch, "image": image, "quality": quality, "group": group, "review": review}


class OrganizationExportPayloadTests(ExportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("export-owner")
        self.organization = create_organization(name="Export Co", owner=self.owner)
        self.content = self.build_org_content(self.owner, self.organization)

        self.other_owner = self.make_user("other-export-owner")
        self.other_org = create_organization(name="Other Export Co", owner=self.other_owner)
        self.build_org_content(self.other_owner, self.other_org, name="b.jpg")

    def test_the_payload_includes_this_orgs_content(self):
        payload = build_organization_export_payload(self.organization)

        self.assertEqual(len(payload["batches"]), 1)
        self.assertEqual(payload["batches"][0]["id"], self.content["batch"].pk)
        self.assertEqual(len(payload["images"]), 1)
        self.assertEqual(len(payload["quality_decisions"]), 1)
        self.assertEqual(len(payload["identity"]["groups"]), 1)
        self.assertEqual(len(payload["moderator_reviews"]), 1)

    def test_the_payload_never_includes_another_orgs_content(self):
        payload = build_organization_export_payload(self.organization)

        image_ids = {row["id"] for row in payload["images"]}
        self.assertNotIn(
            ImageUpload.objects.get(original_name="b.jpg").pk, image_ids,
        )

    def test_the_payload_excludes_personal_per_user_tables(self):
        """Org membership must never expose a member's personal content."""
        payload = build_organization_export_payload(self.organization)

        for forbidden_key in ("consents", "auth_events", "activity_events", "privacy_requests", "account"):
            self.assertNotIn(forbidden_key, payload)

    def test_the_payload_never_includes_biometric_templates_or_invitation_tokens(self):
        payload = build_organization_export_payload(self.organization)

        serialized = json.dumps(payload, default=str)
        self.assertNotIn("private-centroid", serialized)
        self.assertNotIn("protected_centroid", serialized)
        self.assertNotIn("protected_embedding", serialized)
        self.assertNotIn("token_hash", serialized)


class OrganizationExportEnqueueTests(ExportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("enqueue-owner")
        self.organization = create_organization(name="Enqueue Co", owner=self.owner)

    def test_enqueueing_creates_a_queued_export(self):
        export, created = enqueue_organization_export(organization=self.organization, requested_by=self.owner)

        self.assertTrue(created)
        self.assertEqual(export.status, OrganizationExport.Status.QUEUED)
        self.assertEqual(export.organization, self.organization)
        self.assertTrue(export.task_id)

    def test_a_second_enqueue_while_one_is_in_flight_reuses_it(self):
        first, _ = enqueue_organization_export(organization=self.organization, requested_by=self.owner)

        second, created = enqueue_organization_export(organization=self.organization, requested_by=self.owner)

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(OrganizationExport.objects.filter(organization=self.organization).count(), 1)


class OrganizationExportProcessingTests(ExportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("process-owner")
        self.organization = create_organization(name="Process Co", owner=self.owner)
        self.build_org_content(self.owner, self.organization)
        self.export, _ = enqueue_organization_export(organization=self.organization, requested_by=self.owner)

    def test_processing_writes_a_downloadable_file_and_marks_it_ready(self):
        processed = process_organization_export(self.export.pk, self.export.task_id)

        self.assertEqual(processed.status, OrganizationExport.Status.READY)
        self.assertTrue(processed.export_file.name)
        self.assertGreater(processed.file_size, 0)
        self.assertTrue(processed.file_checksum_sha256)
        self.assertIsNotNone(processed.generated_at)
        self.assertIsNotNone(processed.expires_at)

    def test_processing_writes_an_audit_event(self):
        process_organization_export(self.export.pk, self.export.task_id)

        self.assertTrue(
            OrganizationAuditEvent.objects.filter(
                organization=self.organization,
                action=OrganizationAuditEvent.Action.ORGANIZATION_DATA_EXPORTED,
            ).exists()
        )

    def test_a_stale_task_id_does_not_overwrite_a_newer_run(self):
        result = process_organization_export(self.export.pk, "a-stale-task-id")

        self.assertEqual(result.pk, self.export.pk)
        self.assertNotEqual(result.status, OrganizationExport.Status.READY)


class OrganizationExportApiTests(ExportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("api-owner")
        self.organization = create_organization(name="Api Co", owner=self.owner)
        self.build_org_content(self.owner, self.organization)
        self.owner_client = self.authenticated_client(self.owner)
        self.list_url = reverse(
            "privacy_organizations:organization-export-list-create",
            kwargs={"organization_id": self.organization.pk},
        )

    def test_a_plain_member_cannot_request_an_export(self):
        member = self.make_user("api-member")
        OrganizationMembership.objects.create(organization=self.organization, user=member, role=OrganizationMembership.Role.MEMBER)

        response = self.authenticated_client(member).post(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_unrelated_user_gets_404(self):
        outsider = self.authenticated_client(self.make_user("api-outsider"))

        response = outsider.post(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_owner_can_request_an_export_and_a_second_request_reuses_it(self):
        first = self.owner_client.post(self.list_url)
        second = self.owner_client.post(self.list_url)

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["id"], second.data["id"])

    def test_downloading_before_ready_is_a_conflict(self):
        created = self.owner_client.post(self.list_url).data

        response = self.owner_client.get(
            reverse(
                "privacy_organizations:organization-export-download",
                kwargs={"organization_id": self.organization.pk, "export_id": created["id"]},
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_downloading_once_ready_returns_the_file(self):
        created = self.owner_client.post(self.list_url).data
        process_organization_export(created["id"], OrganizationExport.objects.get(pk=created["id"]).task_id)

        response = self.owner_client.get(
            reverse(
                "privacy_organizations:organization-export-download",
                kwargs={"organization_id": self.organization.pk, "export_id": created["id"]},
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = json.loads(b"".join(response.streaming_content))
        self.assertEqual(payload["organization"]["id"], self.organization.pk)


class OrganizationExportExpiryTests(ExportFixtureMixin, TestCase):
    def setUp(self):
        self.owner = self.make_user("expiry-owner")
        self.organization = create_organization(name="Expiry Co", owner=self.owner)
        self.build_org_content(self.owner, self.organization)
        export, _ = enqueue_organization_export(organization=self.organization, requested_by=self.owner)
        self.export = process_organization_export(export.pk, export.task_id)

    def test_a_past_ttl_ready_export_is_marked_expired_and_its_file_removed(self):
        OrganizationExport.objects.filter(pk=self.export.pk).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1),
        )
        file_name = self.export.export_file.name

        expired_count = expire_due_organization_exports()

        self.assertEqual(expired_count, 1)
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, OrganizationExport.Status.EXPIRED)
        self.assertFalse(self.export.export_file)
        self.assertFalse(self.export.export_file.storage.exists(file_name))

    def test_a_not_yet_due_export_is_left_alone(self):
        expired_count = expire_due_organization_exports()

        self.assertEqual(expired_count, 0)
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, OrganizationExport.Status.READY)

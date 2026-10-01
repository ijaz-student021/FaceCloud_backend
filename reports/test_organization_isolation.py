"""Feature 46: organization membership grants no access to a member's own reports.

Mirrors downloads/test_organization_isolation.py. Every existing isolation test
(``test_owner_isolation_hides_...`` in tests.py) proves two unrelated strangers
cannot see each other's reports. This proves the stronger, previously-untested
claim: being the OWNER or MODERATOR of the same organization as the report
owner is *also* not enough. No view was changed; these confirm the existing
``owner=request.user`` filters already hold for this case.
"""
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from organizations.models import OrganizationMembership
from organizations.services import create_organization
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import ProcessingReport
from .tasks import process_processing_report


class OrganizationMembershipGrantsNoReportAccessTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-reports-org-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

        self.org_owner = User.objects.create_user(
            username="rpt-org-owner", email="rpt-org-owner@example.com", password="StrongPass123!", is_email_verified=True,
        )
        self.organization = create_organization(name="Report Isolation Co", owner=self.org_owner)
        self.member = User.objects.create_user(
            username="rpt-org-member", email="rpt-org-member@example.com", password="StrongPass123!", is_email_verified=True,
        )
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.member,
            role=OrganizationMembership.Role.MEMBER, status=OrganizationMembership.Status.ACTIVE,
        )
        self.moderator = User.objects.create_user(
            username="rpt-org-moderator", email="rpt-org-moderator@example.com", password="StrongPass123!", is_email_verified=True,
        )
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator,
            role=OrganizationMembership.Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE,
        )

        self.batch = UploadBatch.objects.create(
            owner=self.member, organization=self.organization, upload_type="single",
            status="uploaded", total_files=1, accepted_files=1, processing_status="completed",
        )
        self.image = ImageUpload.objects.create(
            owner=self.member, batch=self.batch, original_name="portrait.jpg",
            file_extension=".jpg", mime_type="image/jpeg", file_size=14, status="uploaded",
        )
        self.image.original_file.save("portrait.jpg", ContentFile(b"original-image"), save=True)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.member, image=self.image, status="completed", output_format="jpeg", output_name="portrait.jpg",
        )
        preprocessing.processed_file.save("portrait.jpg", ContentFile(b"standardized-image"), save=True)

        self.client.force_authenticate(self.member)
        self.report = process_processing_report(
            ProcessingReport.objects.create(
                owner=self.member, report_type="image", format="json", image=self.image,
                status="queued", task_id="org-isolation-task",
            ).pk,
            "org-isolation-task",
        )
        self.assertEqual(self.report.status, "ready")

    def assert_no_access(self, user):
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get(reverse("reports:list")).data["count"], 0)
        self.assertEqual(
            self.client.get(reverse("reports:detail", args=[self.report.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(reverse("reports:download", args=[self.report.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                reverse("reports:image-create", args=[self.image.pk]), {"format": "json"}, format="json",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_the_organizations_owner_cannot_see_a_members_reports(self):
        self.assert_no_access(self.org_owner)

    def test_the_organizations_moderator_cannot_see_a_members_reports(self):
        self.assert_no_access(self.moderator)

    def test_the_member_can_still_see_their_own_reports(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(reverse("reports:list")).data["count"], 1)
        self.assertEqual(
            self.client.get(reverse("reports:detail", args=[self.report.pk])).status_code,
            status.HTTP_200_OK,
        )

"""Feature 46: organization membership grants no access to a member's own downloads.

Every existing isolation test (``test_owner_isolation_hides_...`` in tests.py)
proves two unrelated strangers cannot see each other's downloads. This file
proves the stronger, previously-untested claim: being the OWNER or MODERATOR of
the same organization as the resource's owner is *also* not enough -- download
packages and the download history stay personal even to an org's own leadership,
exactly like every other owner-scoped resource in this project. No view here was
changed to reach this result; these tests confirm the existing
``owner=request.user`` queryset filters already hold for this case too.
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

from .models import DownloadPackage
from .tasks import process_download_package


class OrganizationMembershipGrantsNoDownloadAccessTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-downloads-org-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

        self.org_owner = User.objects.create_user(
            username="dl-org-owner", email="dl-org-owner@example.com", password="StrongPass123!", is_email_verified=True,
        )
        self.organization = create_organization(name="Download Isolation Co", owner=self.org_owner)
        self.member = User.objects.create_user(
            username="dl-org-member", email="dl-org-member@example.com", password="StrongPass123!", is_email_verified=True,
        )
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.member,
            role=OrganizationMembership.Role.MEMBER, status=OrganizationMembership.Status.ACTIVE,
        )
        self.moderator = User.objects.create_user(
            username="dl-org-moderator", email="dl-org-moderator@example.com", password="StrongPass123!", is_email_verified=True,
        )
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.moderator,
            role=OrganizationMembership.Role.MODERATOR, status=OrganizationMembership.Status.ACTIVE,
        )

        self.batch = UploadBatch.objects.create(
            owner=self.member, organization=self.organization, upload_type="single",
            status="uploaded", total_files=1, accepted_files=1, processing_status="completed",
        )
        image = ImageUpload.objects.create(
            owner=self.member, batch=self.batch, original_name="portrait.jpg",
            file_extension=".jpg", mime_type="image/jpeg", file_size=14, status="uploaded",
        )
        image.original_file.save("portrait.jpg", ContentFile(b"original-image"), save=True)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.member, image=image, status="completed", output_format="jpeg", output_name="portrait.jpg",
        )
        preprocessing.processed_file.save("portrait.jpg", ContentFile(b"standardized-image"), save=True)

        self.client.force_authenticate(self.member)
        self.package = process_download_package(
            DownloadPackage.objects.create(
                owner=self.member, batch=self.batch, package_type="complete_batch",
                report_format="json", status="queued", task_id="org-isolation-task",
            ).pk,
            "org-isolation-task",
        )
        self.assertEqual(self.package.status, DownloadPackage.Status.READY)

    def assert_no_access(self, user):
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get(reverse("downloads:package-list")).data["count"], 0)
        self.assertEqual(self.client.get(reverse("downloads:history")).data["count"], 0)
        self.assertEqual(
            self.client.get(reverse("downloads:package-detail", args=[self.package.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(reverse("downloads:package-download", args=[self.package.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                reverse("downloads:package-create", args=[self.batch.pk]), {"package_type": "complete_batch"}, format="json",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_the_organizations_owner_cannot_see_a_members_downloads(self):
        self.assert_no_access(self.org_owner)

    def test_the_organizations_moderator_cannot_see_a_members_downloads(self):
        self.assert_no_access(self.moderator)

    def test_the_member_can_still_see_their_own_downloads(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(reverse("downloads:package-list")).data["count"], 1)
        self.assertEqual(
            self.client.get(reverse("downloads:package-detail", args=[self.package.pk])).status_code,
            status.HTTP_200_OK,
        )

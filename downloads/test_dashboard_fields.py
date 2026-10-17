"""Downloads screen: kind, expires_at, image_count and has_active_link on the history list."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from enhancement.models import ImageEditVersion, ImageEnhancementResult
from identities.models import IdentityGroup, IdentityGroupExport
from reports.models import ProcessingReport
from sharing.models import ShareLink
from sharing.services import create_share_link, hash_token, revoke_share_link
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import DownloadEvent, DownloadPackage

User = get_user_model()
Type = DownloadEvent.ResourceType


class DownloadFieldTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dl-owner", email="dl-owner@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="dl-other", email="dl-other@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple", total_files=3)

    def result_for(self, owner, batch=None):
        batch = batch or UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(owner=owner, batch=batch, original_name="x.jpg", status="uploaded")
        pre = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        return ImageEnhancementResult.objects.create(
            owner=owner, image=image, preprocessing_result=pre, requested_level="auto",
            applied_level="medium", status="completed", enhanced_file="users/1/enhanced/x.jpg",
        )

    def event(self, resource_type, resource_id, owner=None, **extra):
        return DownloadEvent.objects.create(
            owner=owner or self.user, resource_type=resource_type, resource_id=resource_id,
            filename="file.bin", file_size=10, **extra,
        )

    def rows(self, **params):
        response = self.client.get(reverse("downloads:history"), params)
        self.assertEqual(response.status_code, 200)
        return {row["id"]: row for row in response.json()["results"]}

    def row_of(self, event):
        return self.rows()[event.pk]

    def package(self, **values):
        defaults = {
            "owner": self.user, "batch": self.batch, "package_type": "complete_batch", "status": "ready",
            "manifest": {"summary": {"included_images": 7}}, "expires_at": timezone.now() + timedelta(days=3),
        }
        defaults.update(values)
        return DownloadPackage.objects.create(**defaults)

    def export(self, **values):
        group = IdentityGroup.objects.create(
            owner=self.user, display_name="Folder", member_count=1, protected_centroid=b"c",
            embedding_dimension=3, provider="test", policy_version="v1",
        )
        defaults = {
            "owner": self.user, "identity_group": group, "status": "ready",
            "manifest": {"summary": {"unique_images": 4}}, "expires_at": timezone.now() + timedelta(days=2),
        }
        defaults.update(values)
        return IdentityGroupExport.objects.create(**defaults)

    def test_existing_fields_are_unchanged_and_the_new_ones_are_added(self):
        event = self.event(Type.ORIGINAL_IMAGE, 1, image_id=1)
        row = self.rows()[event.pk]
        for key in ("id", "resource_type", "resource_id", "batch_id", "image_id", "filename", "file_size", "content_type", "downloaded_at"):
            self.assertIn(key, row)
        for key in ("kind", "expires_at", "image_count", "has_active_link"):
            self.assertIn(key, row)

    def test_kind_by_resource_type(self):
        expected = {
            Type.ORIGINAL_IMAGE: "single", Type.STANDARDIZED_IMAGE: "single", Type.ENHANCED_IMAGE: "single",
            Type.EDITED_IMAGE: "single", Type.BATCH_PACKAGE: "batch", Type.IDENTITY_GROUP_PACKAGE: "batch",
            Type.PROCESSING_REPORT: "report",
        }
        events = {rt: self.event(rt, 999) for rt in expected}
        rows = self.rows()
        for resource_type, kind in expected.items():
            self.assertEqual(rows[events[resource_type].pk]["kind"], kind, resource_type)

    def test_a_single_photo_has_no_expiry_count_or_link_by_default(self):
        result = self.result_for(self.user)
        row = self.row_of(self.event(Type.ENHANCED_IMAGE, result.pk))
        self.assertEqual((row["expires_at"], row["image_count"], row["has_active_link"]), (None, None, False))

    def test_a_batch_package_reports_its_expiry_and_image_count(self):
        package = self.package()
        row = self.row_of(self.event(Type.BATCH_PACKAGE, package.pk))
        self.assertEqual(row["kind"], "batch")
        self.assertEqual(row["image_count"], 7)
        self.assertEqual(row["expires_at"], package.expires_at.isoformat().replace("+00:00", "Z"))
        self.assertFalse(row["has_active_link"])

    def test_an_expired_or_unknown_package_still_lists(self):
        gone = self.event(Type.BATCH_PACKAGE, 424242)
        row = self.rows()[gone.pk]
        self.assertEqual((row["expires_at"], row["image_count"], row["has_active_link"]), (None, None, False))

    def test_a_folder_zip_reports_expiry_count_and_its_share_link(self):
        export = self.export()
        event = self.event(Type.IDENTITY_GROUP_PACKAGE, export.pk)
        self.assertFalse(self.rows()[event.pk]["has_active_link"])
        ShareLink.objects.create(
            owner=self.user, identity_export=export, token_hash=hash_token("t1"),
            expires_at=timezone.now() + timedelta(hours=5),
        )
        row = self.rows()[event.pk]
        self.assertTrue(row["has_active_link"])
        self.assertEqual(row["image_count"], 4)
        self.assertEqual(row["expires_at"], export.expires_at.isoformat().replace("+00:00", "Z"))

    def test_an_image_with_a_live_share_link_shows_the_links_expiry(self):
        result = self.result_for(self.user)
        link, _ = create_share_link(owner=self.user, result=result, ttl_hours=6)
        event = self.event(Type.ENHANCED_IMAGE, result.pk)
        row = self.rows()[event.pk]
        self.assertTrue(row["has_active_link"])
        self.assertEqual(row["expires_at"], link.expires_at.isoformat().replace("+00:00", "Z"))

    def test_revoked_and_expired_links_do_not_count(self):
        result = self.result_for(self.user)
        revoked, _ = create_share_link(owner=self.user, result=result)
        revoke_share_link(revoked)
        lapsed, _ = create_share_link(owner=self.user, result=result)
        ShareLink.objects.filter(pk=lapsed.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        row = self.row_of(self.event(Type.ENHANCED_IMAGE, result.pk))
        self.assertEqual((row["has_active_link"], row["expires_at"]), (False, None))

    def test_an_edit_link_belongs_to_the_edit_not_the_enhanced_result(self):
        result = self.result_for(self.user)
        edit = ImageEditVersion.objects.create(
            owner=self.user, enhancement_result=result, status="completed", edited_file="users/1/edited/e.jpg",
        )
        create_share_link(owner=self.user, result=result, edit=edit)
        enhanced = self.event(Type.ENHANCED_IMAGE, result.pk)
        edited = self.event(Type.EDITED_IMAGE, edit.pk)
        rows = self.rows()
        self.assertFalse(rows[enhanced.pk]["has_active_link"])
        self.assertTrue(rows[edited.pk]["has_active_link"])

    def test_a_batch_report_counts_the_batchs_images_and_an_image_report_does_not(self):
        for index in range(3):
            ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name=f"{index}.jpg", status="uploaded")
        batch_report = ProcessingReport.objects.create(owner=self.user, report_type="batch", format="pdf", batch=self.batch, status="ready")
        image = self.batch.images.first()
        image_report = ProcessingReport.objects.create(owner=self.user, report_type="image", format="pdf", image=image, status="ready")
        batch_event = self.event(Type.PROCESSING_REPORT, batch_report.pk)
        image_event = self.event(Type.PROCESSING_REPORT, image_report.pk)
        rows = self.rows()
        batch_row, image_row = rows[batch_event.pk], rows[image_event.pk]
        self.assertEqual((batch_row["kind"], batch_row["image_count"]), ("report", 3))
        self.assertEqual((image_row["kind"], image_row["image_count"]), ("report", None))

    def test_no_token_or_link_address_appears_anywhere_in_the_list(self):
        result = self.result_for(self.user)
        _, token = create_share_link(owner=self.user, result=result)
        self.event(Type.ENHANCED_IMAGE, result.pk)
        text = self.client.get(reverse("downloads:history")).content.decode()
        self.assertNotIn(token, text)
        self.assertNotIn("public_url", text)
        self.assertNotIn("token", text)

    def test_other_peoples_events_and_links_never_appear(self):
        theirs = self.result_for(self.other)
        create_share_link(owner=self.other, result=theirs)
        their_event = self.event(Type.ENHANCED_IMAGE, theirs.pk, owner=self.other)
        self.assertNotIn(their_event.pk, self.rows())
        # My history row pointing at someone else's result id must not pick up their link.
        mine = self.event(Type.ENHANCED_IMAGE, theirs.pk)
        row = self.rows()[mine.pk]
        self.assertEqual((row["has_active_link"], row["expires_at"]), (False, None))

    def test_an_empty_history_is_an_empty_page(self):
        body = self.client.get(reverse("downloads:history")).json()
        self.assertEqual((body["count"], body["results"]), (0, []))

    def test_pagination_still_works_and_the_extras_cover_every_page_row(self):
        package = self.package()
        for _ in range(5):
            self.event(Type.BATCH_PACKAGE, package.pk)
        body = self.client.get(reverse("downloads:history"), {"page_size": 2}).json()
        self.assertEqual((body["count"], len(body["results"])), (5, 2))
        self.assertTrue(all(row["image_count"] == 7 for row in body["results"]))

    def test_the_number_of_queries_does_not_grow_with_the_number_of_rows(self):
        package = self.package()
        result = self.result_for(self.user)
        for _ in range(2):
            self.event(Type.BATCH_PACKAGE, package.pk)
            self.event(Type.ENHANCED_IMAGE, result.pk)
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("downloads:history"))
        for _ in range(10):
            self.event(Type.BATCH_PACKAGE, package.pk)
            self.event(Type.ENHANCED_IMAGE, result.pk)
        with self.assertNumQueries(len(small.captured_queries)):
            self.client.get(reverse("downloads:history"))

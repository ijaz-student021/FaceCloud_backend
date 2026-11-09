"""Deleting photographs: folders are recounted, ZIPs and folder exports that hold a copy expire, and the bulk request reports what it did."""
from datetime import timedelta

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from downloads.models import DownloadPackage
from enhancement.models import EnhancementLevel, ImageEnhancementResult
from identities.models import IdentityGroup, IdentityGroupExport, IdentityMembership
from identities.test_person_folders import FolderFixtures
from sharing.models import ShareLink

from .models import ImageUpload, UploadBatch


class DeletionTests(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("deleter")
        self.other = self.make_user("bystander")
        self.group = self.make_group(self.owner)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def delete(self, image):
        return self.client.delete(reverse("image-upload-detail", args=[image.pk]))

    def test_deleting_a_photo_recounts_its_folder(self):
        first, _ = self.add_photo(self.group, "a.jpg")
        self.add_photo(self.group, "b.jpg")
        self.group.refresh_from_db()
        self.assertEqual(self.client.delete(reverse("image-upload-detail", args=[first.pk])).status_code, 204)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 1)
        self.assertEqual(self.group.memberships.count(), 1)

    def test_deleting_the_cover_photo_clears_the_cover_and_keeps_the_folders_average_face_of_the_rest(self):
        image, membership = self.add_photo(self.group)
        self.add_photo(self.group, "rest.jpg")
        IdentityGroup.objects.filter(pk=self.group.pk).update(profile_membership=membership)
        self.delete(image)
        self.group.refresh_from_db()
        self.assertEqual((self.group.member_count, self.group.profile_membership_id), (1, None))
        self.assertNotEqual(bytes(self.group.protected_centroid), b"")

    def test_deleting_the_last_photo_deletes_the_folder(self):
        image, membership = self.add_photo(self.group)
        IdentityGroup.objects.filter(pk=self.group.pk).update(profile_membership=membership)
        self.delete(image)
        self.assertFalse(IdentityGroup.objects.filter(pk=self.group.pk).exists())          # no empty folder is left behind (identities/test_empty_folders.py)

    def test_the_ready_export_of_a_folder_that_goes_with_its_last_photo_goes_too(self):
        image, _ = self.add_photo(self.group)
        export = IdentityGroupExport.objects.create(
            owner=self.owner, identity_group=self.group, status="ready", file_size=5, expires_at=timezone.now() + timedelta(days=1),
        )
        export.export_file.save("e.zip", ContentFile(b"zipzip"))
        self.delete(image)
        self.assertFalse(IdentityGroupExport.objects.filter(pk=export.pk).exists())

    def test_a_ready_package_of_the_same_upload_is_expired_and_its_file_removed(self):
        image, _ = self.add_photo(self.group)
        package = DownloadPackage.objects.create(
            owner=self.owner, batch=image.batch, package_type="accepted_results", status="ready",
            file_size=5, expires_at=timezone.now() + timedelta(days=1),
        )
        package.package_file.save("p.zip", ContentFile(b"zipzip"))
        stored = package.package_file.name
        untouched = DownloadPackage.objects.create(
            owner=self.owner, batch=UploadBatch.objects.create(owner=self.owner, upload_type="single"),
            package_type="accepted_results", status="ready", expires_at=timezone.now() + timedelta(days=1),
        )
        self.delete(image)
        package.refresh_from_db()
        self.assertEqual((package.status, package.file_size, package.package_file.name), ("expired", 0, ""))
        self.assertFalse(package.package_file.storage.exists(stored))
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, "ready")                                      # another upload's package is not touched

    def test_a_ready_folder_export_and_its_secure_link_stop_working(self):
        image, _ = self.add_photo(self.group)
        self.add_photo(self.group, "stays.jpg")           # the folder keeps a photograph, so it (and its expired export) remain
        export = IdentityGroupExport.objects.create(
            owner=self.owner, identity_group=self.group, status="ready", file_size=5, expires_at=timezone.now() + timedelta(days=1),
        )
        export.export_file.save("e.zip", ContentFile(b"zipzip"))
        link = ShareLink.objects.create(
            owner=self.owner, identity_export=export, token_hash="x" * 64, expires_at=timezone.now() + timedelta(days=1),
        )
        self.delete(image)
        export.refresh_from_db()
        self.assertEqual((export.status, export.export_file.name), ("expired", ""))
        self.assertTrue(ShareLink.objects.filter(pk=link.pk).exists())                  # the link remains, and answers 410 as for any expired ZIP

    def test_the_share_link_of_a_deleted_photos_enhanced_copy_goes_with_it(self):
        image, _ = self.add_photo(self.group)
        pre = image.preprocessing_result
        result = ImageEnhancementResult.objects.create(
            owner=self.owner, image=image, preprocessing_result=pre, status="completed", requested_level=EnhancementLevel.AUTO,
        )
        link = ShareLink.objects.create(
            owner=self.owner, enhancement_result=result, token_hash="y" * 64, expires_at=timezone.now() + timedelta(days=1),
        )
        self.delete(image)
        self.assertFalse(ShareLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(ImageEnhancementResult.objects.filter(pk=result.pk).exists())

    def test_deleting_an_upload_batch_does_the_same(self):
        image, _ = self.add_photo(self.group)
        self.add_photo(self.group, "keep.jpg")
        response = self.client.delete(reverse("upload-batch-detail", args=[image.batch_id]))
        self.assertEqual(response.status_code, 204)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 1)


class BulkDeleteTests(FolderFixtures, TestCase):
    def setUp(self):
        self.owner = self.make_user("bulk-owner")
        self.other = self.make_user("bulk-other")
        self.group = self.make_group(self.owner)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = reverse("image-bulk-delete")

    def test_it_deletes_the_callers_photos_and_reports_the_rest_as_not_found(self):
        mine = [self.add_photo(self.group, f"m{i}.jpg")[0] for i in range(3)]
        theirs, _ = self.add_photo(self.make_group(self.other, "Theirs"), "t.jpg")
        response = self.client.post(self.url, {"image_ids": [mine[0].pk, mine[1].pk, theirs.pk, 999999, mine[0].pk]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["deleted_ids"], [mine[0].pk, mine[1].pk])
        self.assertEqual(body["not_found_ids"], [theirs.pk, 999999])
        self.assertEqual(body["folders_updated"], 1)
        self.assertTrue(ImageUpload.objects.filter(pk=theirs.pk).exists())               # someone else's photo is never touched
        self.assertEqual(ImageUpload.objects.filter(owner=self.owner).count(), 1)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 1)

    def test_it_validates_the_request(self):
        for body in ({}, {"image_ids": []}, {"image_ids": ["a"]}, {"image_ids": [0]}, {"image_ids": list(range(1, 202))}):
            self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400, body)

    def test_it_needs_a_sign_in(self):
        self.assertEqual(APIClient().post(self.url, {"image_ids": [1]}, format="json").status_code, 401)

    def test_a_folder_is_recounted_once_however_many_photos_leave_it(self):
        photos = [self.add_photo(self.group, f"q{i}.jpg")[0] for i in range(4)]
        response = self.client.post(self.url, {"image_ids": [p.pk for p in photos]}, format="json")
        body = response.json()
        self.assertEqual((body["folders_updated"], body["folders_deleted"]), (1, 1))
        self.assertFalse(IdentityGroup.objects.filter(pk=self.group.pk).exists())          # all four photographs left it: the folder goes
        self.assertFalse(IdentityMembership.objects.filter(identity_group=self.group).exists())

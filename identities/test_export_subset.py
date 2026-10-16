"""Exporting only some of a person folder's photos, and sharing the finished ZIP."""
import io
import json
import shutil
import tempfile
import zipfile
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User

from .models import IdentityFace, IdentityGroup, IdentityGroupExport, IdentityMembership
from .tasks import process_identity_group_export
from . import test_exports


class FolderExportFixtures(APITestCase):
    """A folder of three photos, plus a photo in another folder and one that belongs to someone else."""

    _image_and_analysis = test_exports.SecureIdentityGroupExportTests._image_and_analysis

    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-export-subset-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.owner = User.objects.create_user(
            username="subset-owner", email="subset-owner@example.com", password="StrongPass123!", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="subset-other", email="subset-other@example.com", password="StrongPass123!", is_email_verified=True,
        )
        self.client.force_authenticate(self.owner)
        self.group = self.make_group(self.owner, "Folder")
        self.elsewhere = self.make_group(self.owner, "Elsewhere")
        self.image_a, self.member_a = self.add_member(self.group, "a.jpg")
        self.image_b, self.member_b = self.add_member(self.group, "b.jpg")
        self.image_c, self.member_c = self.add_member(self.group, "c.jpg")
        self.image_elsewhere, _ = self.add_member(self.elsewhere, "elsewhere.jpg")
        self.image_theirs, _ = self.add_member(self.make_group(self.other, "Theirs"), "theirs.jpg")
        self.url = reverse("identities:group-export-list-create", args=[self.group.pk])

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def make_group(self, owner, name):
        return IdentityGroup.objects.create(
            owner=owner, display_name=name, member_count=1, protected_centroid=b"c",
            embedding_dimension=3, provider="test", policy_version="test-v1",
        )

    def add_member(self, group, filename):
        image, analysis = self._image_and_analysis(group.owner, filename, f"bytes of {filename}".encode())
        face = IdentityFace.objects.create(
            owner=group.owner, image=image, analysis=analysis, face_index=0,
            protected_embedding=b"e", embedding_dimension=3, provider="test",
        )
        membership = IdentityMembership.objects.create(
            owner=group.owner, identity_face=face, identity_group=group,
            state=IdentityMembership.State.CONFIRMED, decision_method="test",
        )
        return image, membership

    def post_export(self, **body):
        with patch("identities.tasks.generate_identity_group_export_task.apply_async"):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(self.url, {"include_report": True, **body}, format="json")

    def build(self, export):
        built = process_identity_group_export(export.pk, export.task_id)
        with built.export_file.open("rb") as handle:
            archive = zipfile.ZipFile(io.BytesIO(handle.read()))
        return built, archive


class ExportSubsetTests(FolderExportFixtures):
    def test_without_image_ids_everything_in_the_folder_is_exported(self):
        response = self.post_export()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        export = IdentityGroupExport.objects.get(pk=response.data["export"]["id"])
        self.assertEqual(export.image_ids, [])
        self.assertEqual(response.data["export"]["image_ids"], [])
        _, archive = self.build(export)
        manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["summary"]["unique_images"], 3)

    def test_image_ids_export_only_those_photos(self):
        response = self.post_export(image_ids=[self.image_c.pk, self.image_a.pk, self.image_a.pk])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["export"]["image_ids"], sorted([self.image_a.pk, self.image_c.pk]))
        export = IdentityGroupExport.objects.get(pk=response.data["export"]["id"])
        _, archive = self.build(export)
        manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["summary"]["unique_images"], 2)
        self.assertEqual({item["image_id"] for item in manifest["files"]}, {self.image_a.pk, self.image_c.pk})
        self.assertEqual(manifest["options"]["image_ids"], sorted([self.image_a.pk, self.image_c.pk]))
        contents = b"".join(archive.read(name) for name in archive.namelist())
        self.assertIn(b"bytes of a.jpg", contents)
        self.assertNotIn(b"bytes of b.jpg", contents)

    def test_a_photo_that_is_not_in_the_folder_is_a_400_and_nothing_is_created(self):
        for outside in (self.image_elsewhere.pk, self.image_theirs.pk, 999999):
            response = self.post_export(image_ids=[self.image_a.pk, outside])
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, outside)
            self.assertIn("image_ids", response.json())
            self.assertIn(str(outside), response.json()["image_ids"][0])
        self.assertFalse(IdentityGroupExport.objects.exists())

    def test_a_photo_removed_from_the_folder_is_no_longer_selectable(self):
        IdentityMembership.objects.filter(pk=self.member_b.pk).update(state=IdentityMembership.State.REMOVED)
        self.assertEqual(self.post_export(image_ids=[self.image_b.pk]).status_code, status.HTTP_400_BAD_REQUEST)

    def test_bad_image_ids_are_400(self):
        for bad in ([], [0], ["x"], "1"):
            self.assertEqual(self.post_export(image_ids=bad).status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_null_image_ids_means_everything(self):
        response = self.post_export(image_ids=None)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["export"]["image_ids"], [])

    def test_the_same_selection_is_deduplicated_but_a_different_one_is_a_new_export(self):
        first = self.post_export(image_ids=[self.image_a.pk, self.image_b.pk])
        same = self.post_export(image_ids=[self.image_b.pk, self.image_a.pk])
        other = self.post_export(image_ids=[self.image_a.pk])
        everything = self.post_export()
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(same.status_code, status.HTTP_200_OK)
        self.assertFalse(same.data["created"])
        self.assertEqual(same.data["export"]["id"], first.data["export"]["id"])
        self.assertEqual(other.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(everything.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(IdentityGroupExport.objects.count(), 3)

    def test_regenerating_keeps_the_original_selection(self):
        response = self.post_export(image_ids=[self.image_a.pk])
        export = IdentityGroupExport.objects.get(pk=response.data["export"]["id"])
        IdentityGroupExport.objects.filter(pk=export.pk).update(status="failed")
        with patch("identities.tasks.generate_identity_group_export_task.apply_async"):
            with self.captureOnCommitCallbacks(execute=True):
                again = self.client.post(reverse("identities:group-export-regenerate", args=[export.pk]), {}, format="json")
        self.assertEqual(again.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(again.data["export"]["image_ids"], [self.image_a.pk])

    def test_a_photo_removed_after_the_request_drops_out_of_the_archive(self):
        response = self.post_export(image_ids=[self.image_a.pk, self.image_b.pk])
        export = IdentityGroupExport.objects.get(pk=response.data["export"]["id"])
        IdentityMembership.objects.filter(pk=self.member_b.pk).update(state=IdentityMembership.State.REMOVED)
        _, archive = self.build(export)
        manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual({item["image_id"] for item in manifest["files"]}, {self.image_a.pk})

    def test_another_users_folder_is_404_even_with_valid_looking_ids(self):
        self.client.force_authenticate(self.other)
        response = self.post_export(image_ids=[self.image_a.pk])
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

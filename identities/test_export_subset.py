"""Exporting only some of a person folder's photos, and sharing the finished ZIP."""
import io
import json
import shutil
import tempfile
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from activity.models import ActivityEvent
from sharing.models import ShareLink

from .export_services import expire_identity_export_if_needed
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


class ExportShareTests(FolderExportFixtures):
    def ready_export(self, **overrides):
        values = {
            "owner": self.owner, "identity_group": self.group, "status": "queued", "task_id": "share-task",
        }
        values.update(overrides)
        export = IdentityGroupExport.objects.create(**values)
        return process_identity_group_export(export.pk, "share-task")

    def share_url(self, export):
        return reverse("identities:group-export-share", args=[export.pk])

    def share(self, export, **body):
        return self.client.post(self.share_url(export), body, format="json")

    def public(self, token):
        return APIClient().get(reverse("sharing:public-view", kwargs={"token": token}))

    def test_sharing_a_ready_export_returns_the_link_and_the_token_once(self):
        export = self.ready_export()
        response = self.share(export, ttl_hours=2, label="For Sara")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["identity_export"], export.pk)
        self.assertIsNone(body["result"])
        self.assertEqual(body["label"], "For Sara")
        self.assertEqual(body["state"], "active")
        self.assertIn(body["token"], body["public_url"])
        self.assertIn("/info/", body["info_url"])
        for key in ("id", "edit", "watermark", "expires_at", "revoked_at", "access_count", "last_accessed_at", "created_at"):
            self.assertIn(key, body)
        link = ShareLink.objects.get(pk=body["id"])
        self.assertNotEqual(link.token_hash, body["token"])
        self.assertEqual(link.identity_export_id, export.pk)

    def test_the_public_link_downloads_the_zip_without_a_login(self):
        export = self.ready_export()
        token = self.share(export).json()["token"]
        response = self.public(token)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"PK"))
        link = ShareLink.objects.get()
        self.assertEqual(link.access_count, 1)
        info = APIClient().get(reverse("sharing:public-info", kwargs={"token": token})).json()
        self.assertEqual(info["content_type"], "application/zip")
        self.assertFalse(info["watermarked"])

    def test_a_subset_export_shares_just_that_zip(self):
        export = self.ready_export(image_ids=[self.image_a.pk], selection_key="x")
        token = self.share(export).json()["token"]
        archive = zipfile.ZipFile(io.BytesIO(b"".join(self.public(token).streaming_content)))
        manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["summary"]["unique_images"], 1)

    def test_list_and_detail_never_return_the_token(self):
        export = self.ready_export()
        created = self.share(export).json()
        listing = self.client.get(reverse("sharing:link-list-create")).json()
        detail = self.client.get(reverse("sharing:link-detail", args=[created["id"]])).json()
        for payload in (listing, detail):
            text = json.dumps(payload)
            self.assertNotIn(created["token"], text)
            self.assertNotIn("public_url", text)
        rows = listing["results"] if isinstance(listing, dict) else listing
        self.assertEqual(rows[0]["identity_export"], export.pk)
        self.assertIsNone(rows[0]["result"])

    def test_revoking_stops_the_link_at_once(self):
        export = self.ready_export()
        created = self.share(export).json()
        self.assertEqual(self.public(created["token"]).status_code, status.HTTP_200_OK)
        revoked = self.client.post(reverse("sharing:link-revoke", args=[created["id"]]))
        self.assertEqual(revoked.status_code, status.HTTP_200_OK)
        self.assertEqual(revoked.json()["state"], "revoked")
        self.assertEqual(self.public(created["token"]).status_code, status.HTTP_410_GONE)

    def test_another_user_cannot_revoke_or_see_the_link(self):
        created = self.share(self.ready_export()).json()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(reverse("sharing:link-revoke", args=[created["id"]])).status_code, 404)
        self.assertEqual(self.client.get(reverse("sharing:link-detail", args=[created["id"]])).status_code, 404)
        self.assertEqual(self.public(created["token"]).status_code, status.HTTP_200_OK)

    def test_an_expired_link_is_gone_and_an_unknown_token_is_404(self):
        created = self.share(self.ready_export()).json()
        ShareLink.objects.filter(pk=created["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.public(created["token"]).status_code, status.HTTP_410_GONE)
        self.assertEqual(self.public("not-a-real-token").status_code, status.HTTP_404_NOT_FOUND)

    def test_the_link_never_outlives_the_zip(self):
        export = self.ready_export()
        IdentityGroupExport.objects.filter(pk=export.pk).update(expires_at=timezone.now() + timedelta(hours=2))
        created = self.share(export, ttl_hours=72).json()
        link = ShareLink.objects.get(pk=created["id"])
        self.assertLessEqual(link.expires_at, IdentityGroupExport.objects.get(pk=export.pk).expires_at)

    def test_when_the_zip_expires_the_link_stops_working(self):
        export = self.ready_export()
        token = self.share(export).json()["token"]
        IdentityGroupExport.objects.filter(pk=export.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.public(token).status_code, status.HTTP_410_GONE)
        expire_identity_export_if_needed(IdentityGroupExport.objects.get(pk=export.pk))
        self.assertEqual(self.public(token).status_code, status.HTTP_410_GONE)

    def test_only_a_finished_unexpired_export_can_be_shared(self):
        queued = IdentityGroupExport.objects.create(
            owner=self.owner, identity_group=self.group, status="queued", task_id="t", report_format="csv",
        )
        self.assertEqual(self.share(queued).status_code, status.HTTP_409_CONFLICT)
        expired = self.ready_export()
        IdentityGroupExport.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.share(expired).status_code, status.HTTP_410_GONE)
        self.assertFalse(ShareLink.objects.exists())

    def test_another_users_export_is_404(self):
        export = self.ready_export()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.share(export).status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(ShareLink.objects.exists())

    def test_the_lifetime_is_bounded(self):
        export = self.ready_export()
        too_long = settings.SHARE_LINK_MAX_TTL_HOURS + 1
        self.assertEqual(self.share(export, ttl_hours=too_long).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.share(export, ttl_hours=0).status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(SHARE_LINK_MAX_ACTIVE_PER_USER=1)
    def test_the_per_user_limit_on_active_links_applies(self):
        export = self.ready_export()
        self.assertEqual(self.share(export).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.share(export).status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_creating_and_revoking_are_audited(self):
        created = self.share(self.ready_export()).json()
        self.client.post(reverse("sharing:link-revoke", args=[created["id"]]))
        events = set(ActivityEvent.objects.filter(user=self.owner, category="share").values_list("event", flat=True))
        self.assertEqual(events, {"share_link_created", "share_link_revoked"})

    def test_image_share_links_keep_their_shape(self):
        # An image link still has a result and a null identity_export.
        enhancement = self.image_a.enhancement_results.first()
        response = self.client.post(reverse("sharing:link-list-create"), {"result": enhancement.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["result"], enhancement.pk)
        self.assertIsNone(body["identity_export"])

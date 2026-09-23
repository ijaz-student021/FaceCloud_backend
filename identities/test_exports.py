import io
import json
import shutil
import tempfile
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from downloads.models import DownloadEvent
from enhancement.models import ImageEnhancementResult
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .models import (IdentityAnalysis, IdentityFace, IdentityGroup,
                     IdentityGroupExport, IdentityMembership)
from .tasks import process_identity_group_export


class SecureIdentityGroupExportTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="facecloud-identity-exports-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.owner = User.objects.create_user(
            username="identity-export-owner",
            email="identity-export-owner@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="identity-export-other",
            email="identity-export-other@example.com",
            password="StrongPass123!",
            is_email_verified=True,
        )
        self.client.force_authenticate(self.owner)
        self.group = IdentityGroup.objects.create(
            owner=self.owner,
            display_name="../ Family / Person",
            member_count=2,
            protected_centroid=b"private-centroid",
            embedding_dimension=3,
            provider="test",
            policy_version="test-v1",
        )
        self.image, analysis = self._image_and_analysis(
            self.owner, "../ private portrait.jpg", b"owner-original"
        )
        for face_index in (0, 1):
            face = IdentityFace.objects.create(
                owner=self.owner,
                image=self.image,
                analysis=analysis,
                face_index=face_index,
                protected_embedding=b"encrypted-private-embedding",
                embedding_dimension=3,
                provider="test",
            )
            IdentityMembership.objects.create(
                owner=self.owner,
                identity_face=face,
                identity_group=self.group,
                state=IdentityMembership.State.NEW_GROUP,
                decision_method="test",
            )

        other_image, other_analysis = self._image_and_analysis(
            self.other, "other-user-secret.jpg", b"other-user-secret-bytes"
        )
        other_face = IdentityFace.objects.create(
            owner=self.other,
            image=other_image,
            analysis=other_analysis,
            face_index=0,
            protected_embedding=b"other-private-embedding",
            embedding_dimension=3,
            provider="test",
        )
        # Deliberately inconsistent database row proves archive queries enforce
        # owner equality even if data was inserted without model.full_clean().
        IdentityMembership.objects.create(
            owner=self.owner,
            identity_face=other_face,
            identity_group=self.group,
            state=IdentityMembership.State.NEW_GROUP,
            decision_method="malicious-test-row",
        )

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _image_and_analysis(self, owner, filename, original_bytes):
        batch = UploadBatch.objects.create(
            owner=owner,
            upload_type="single",
            status="uploaded",
            processing_status="completed",
            current_stage="completed",
        )
        image = ImageUpload.objects.create(
            owner=owner,
            batch=batch,
            original_name=filename,
            status="uploaded",
        )
        image.original_file.save(filename, ContentFile(original_bytes), save=True)
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=owner,
            image=image,
            status="completed",
            output_format="jpeg",
            output_name="standardized.jpg",
        )
        preprocessing.processed_file.save(
            "standardized.jpg", ContentFile(b"standardized-owner-image"), save=True
        )
        validation = FaceValidationResult.objects.create(
            owner=owner,
            image=image,
            preprocessing_result=preprocessing,
            decision="accepted",
            face_count=2,
            faces=[],
        )
        analysis = IdentityAnalysis.objects.create(
            owner=owner,
            image=image,
            preprocessing_result=preprocessing,
            face_validation=validation,
            status="completed",
            provider="test",
            policy_version="test-v1",
        )
        enhancement = ImageEnhancementResult.objects.create(
            owner=owner,
            image=image,
            preprocessing_result=preprocessing,
            requested_level="auto",
            applied_level="medium",
            status="completed",
            output_name="enhanced.jpg",
            output_format="jpeg",
        )
        enhancement.enhanced_file.save(
            "enhanced.jpg", ContentFile(b"enhanced-owner-image"), save=True
        )
        return image, analysis

    def _export(self, **overrides):
        values = {
            "owner": self.owner,
            "identity_group": self.group,
            "include_originals": True,
            "include_preprocessed": True,
            "include_enhanced": True,
            "include_report": True,
            "report_format": "json",
            "status": "queued",
            "task_id": "direct-export-task",
        }
        values.update(overrides)
        return IdentityGroupExport.objects.create(**values)

    @staticmethod
    def _zip_document(export):
        with export.export_file.open("rb") as export_file:
            return zipfile.ZipFile(io.BytesIO(export_file.read()))

    def test_archive_deduplicates_images_and_excludes_other_owner_data(self):
        export = process_identity_group_export(
            self._export().pk, "direct-export-task"
        )
        self.assertEqual(export.status, "ready")
        self.assertEqual(len(export.file_checksum_sha256), 64)
        self.assertTrue(export.export_file.name.startswith(
            f"users/{self.owner.pk}/identity-exports/"
        ))
        with self._zip_document(export) as archive:
            names = archive.namelist()
            contents = b"".join(archive.read(name) for name in names)
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(len([name for name in names if name.startswith("originals/")]), 1)
        self.assertEqual(manifest["summary"]["unique_images"], 1)
        self.assertNotIn(b"other-user-secret", contents)
        self.assertNotIn(b"private-centroid", contents)
        self.assertNotIn(b"private-embedding", contents)
        self.assertTrue(all(".." not in name and not name.startswith("/") for name in names))

    def test_create_reuses_active_export_and_validates_nonempty_options(self):
        url = reverse("identities:group-export-list-create", args=[self.group.pk])
        body = {
            "include_originals": True,
            "include_preprocessed": False,
            "include_enhanced": False,
            "include_report": True,
            "report_format": "csv",
        }
        with patch("identities.tasks.generate_identity_group_export_task.apply_async") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, body, format="json")
            duplicate = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(response.data["created"])
        self.assertEqual(duplicate.status_code, status.HTTP_200_OK)
        self.assertFalse(duplicate.data["created"])
        queued.assert_called_once()
        invalid = self.client.post(url, {
            "include_originals": False,
            "include_preprocessed": False,
            "include_enhanced": False,
            "include_report": False,
        }, format="json")
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_user_cannot_see_create_download_delete_or_regenerate(self):
        export = process_identity_group_export(
            self._export().pk, "direct-export-task"
        )
        self.client.force_authenticate(self.other)
        protected_requests = (
            self.client.get(reverse("identities:group-export-list-create", args=[self.group.pk])),
            self.client.post(reverse("identities:group-export-list-create", args=[self.group.pk]), {}, format="json"),
            self.client.get(reverse("identities:group-export-detail", args=[export.pk])),
            self.client.get(reverse("identities:group-export-download", args=[export.pk])),
            self.client.delete(reverse("identities:group-export-detail", args=[export.pk])),
            self.client.post(reverse("identities:group-export-regenerate", args=[export.pk]), {}, format="json"),
        )
        self.assertTrue(all(response.status_code == status.HTTP_404_NOT_FOUND for response in protected_requests))
        self.assertTrue(IdentityGroupExport.objects.filter(pk=export.pk).exists())
        self.assertEqual(DownloadEvent.objects.filter(owner=self.other).count(), 0)

    def test_owner_download_streams_zip_and_records_owner_history(self):
        export = process_identity_group_export(
            self._export().pk, "direct-export-task"
        )
        response = self.client.get(
            reverse("identities:group-export-download", args=[export.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(b"".join(response.streaming_content).startswith(b"PK"))
        event = DownloadEvent.objects.get(resource_type="identity_group_package")
        self.assertEqual(event.owner, self.owner)
        self.assertEqual(event.resource_id, export.pk)

    def test_expired_export_is_deleted_and_cannot_be_downloaded(self):
        export = process_identity_group_export(
            self._export().pk, "direct-export-task"
        )
        stored_name = export.export_file.name
        storage = export.export_file.storage
        export.expires_at = timezone.now() - timedelta(seconds=1)
        export.save(update_fields=["expires_at", "updated_at"])
        response = self.client.get(
            reverse("identities:group-export-download", args=[export.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        export.refresh_from_db()
        self.assertEqual(export.status, "expired")
        self.assertFalse(storage.exists(stored_name))

    @override_settings(DOWNLOAD_PACKAGE_MAX_UNCOMPRESSED_SIZE=1)
    def test_size_limit_fails_without_partial_file_or_private_error(self):
        export = process_identity_group_export(
            self._export().pk, "direct-export-task"
        )
        self.assertEqual(export.status, "failed")
        self.assertFalse(export.export_file)
        self.assertEqual(
            export.failure_reason,
            "The identity-group export could not be generated safely.",
        )


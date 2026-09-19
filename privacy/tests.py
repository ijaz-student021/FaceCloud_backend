import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from activity.models import ActivityEvent
from enhancement.models import ImageEnhancementResult
from enhancement.services import enhance_bgr, process_enhancement_result
from identities.models import (
    IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup,
    IdentityGroupExport, IdentityMembership,
)
from identities.services import enqueue_identity_analysis, process_identity_analysis
from identities.storage import is_current_payload, payload_format, protect_embedding, reveal_embedding
from identities.test_embedding_crypto import LEGACY_KEY, LEGACY_PAYLOAD
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .checks import privacy_configuration_check, privacy_deployment_check
from .models import BiometricConsent, ConsentPurpose, PrivacyRequest
from .services import (
    audit_report, biometric_processing_allowed, build_privacy_export, erase_facial_data,
    embedding_encryption_status, facial_data_counts, has_active_consent, purge_expired_facial_data,
    record_consent, reencrypt_identity_embeddings,
)
from .tasks import purge_expired_facial_data_task

User = get_user_model()
PURPOSE = ConsentPurpose.FACE_IDENTITY
POLICY = "test-policy-v1"


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="OrchidRiver!4729",
        is_email_verified=True, **extra,
    )


def seed_identity_data(owner, tag, vectors=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))):
    batch = UploadBatch.objects.create(owner=owner, upload_type="single")
    image = ImageUpload.objects.create(owner=owner, batch=batch, original_name=f"{tag}.jpg", status="uploaded")
    preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
    validation = FaceValidationResult.objects.create(
        owner=owner, image=image, preprocessing_result=preprocessing, decision="accepted",
        face_count=len(vectors), faces=[{"x": 0, "y": 0, "width": 10, "height": 10}] * len(vectors),
    )
    analysis = IdentityAnalysis.objects.create(
        owner=owner, image=image, preprocessing_result=preprocessing, face_validation=validation,
        status="completed", provider="test", policy_version="test-v1",
    )
    faces = [
        IdentityFace.objects.create(
            owner=owner, image=image, analysis=analysis, face_index=index,
            protected_embedding=protect_embedding(vector), embedding_dimension=3, provider="test",
        )
        for index, vector in enumerate(vectors)
    ]
    return image, analysis, faces


def make_group(owner, name, faces):
    group = IdentityGroup.objects.create(
        owner=owner, display_name=name, member_count=len(faces), embedding_dimension=3,
        provider="test", policy_version="test-v1",
        protected_centroid=protect_embedding([1.0, 0.0, 0.0]),
    )
    for face in faces:
        IdentityMembership.objects.create(
            owner=owner, identity_face=face, identity_group=group, state="confirmed", decision_method="test",
        )
    IdentityChange.objects.create(
        owner=owner, actor=owner, group=group, action="renamed", before={"name": "a"}, after={"name": name},
    )
    return group


def make_export(owner, group):
    export = IdentityGroupExport.objects.create(owner=owner, identity_group=group, status="completed")
    export.export_file.save("bundle.zip", ContentFile(b"zip-bytes"))
    return export


class MediaIsolatedTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(
            MEDIA_ROOT=self._media.name, IDENTITY_EMBEDDING_KEY="privacy-test-key",
            BIOMETRIC_CONSENT_POLICY_VERSION=POLICY,
        )
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        self._media.cleanup()
        super().tearDown()


class ConsentServiceTests(MediaIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("consenter")

    def test_grant_withdraw_history_is_append_only_and_idempotent(self):
        row, created = record_consent(self.user, PURPOSE, True, POLICY)
        self.assertTrue(created)
        again, created_again = record_consent(self.user, PURPOSE, True, POLICY)
        self.assertFalse(created_again)
        self.assertEqual(again.pk, row.pk)
        record_consent(self.user, PURPOSE, False, "")
        self.assertEqual(BiometricConsent.objects.filter(user=self.user).count(), 2)
        self.assertFalse(has_active_consent(self.user))
        record_consent(self.user, PURPOSE, True, POLICY)
        self.assertEqual(BiometricConsent.objects.filter(user=self.user).count(), 3)
        self.assertTrue(has_active_consent(self.user))

    def test_granting_against_an_outdated_policy_is_rejected(self):
        with self.assertRaises(ValidationError):
            record_consent(self.user, PURPOSE, True, "older-policy")
        self.assertFalse(BiometricConsent.objects.exists())

    def test_unknown_purpose_is_rejected(self):
        with self.assertRaises(ValidationError):
            record_consent(self.user, "advertising", True, POLICY)

    def test_a_new_policy_version_invalidates_earlier_consent(self):
        record_consent(self.user, PURPOSE, True, POLICY)
        self.assertTrue(has_active_consent(self.user))
        with override_settings(BIOMETRIC_CONSENT_POLICY_VERSION="test-policy-v2"):
            self.assertFalse(has_active_consent(self.user))

    def test_processing_is_only_gated_when_consent_is_required(self):
        with override_settings(BIOMETRIC_CONSENT_REQUIRED=False):
            self.assertTrue(biometric_processing_allowed(self.user))
        with override_settings(BIOMETRIC_CONSENT_REQUIRED=True):
            self.assertFalse(biometric_processing_allowed(self.user))
            record_consent(self.user, PURPOSE, True, POLICY)
            self.assertTrue(biometric_processing_allowed(self.user))

    def test_consent_changes_appear_in_the_activity_timeline(self):
        record_consent(self.user, PURPOSE, True, POLICY)
        record_consent(self.user, PURPOSE, False, "")
        events = list(
            ActivityEvent.objects.filter(user=self.user, category="privacy")
            .order_by("id").values_list("event", flat=True)
        )
        self.assertEqual(events, ["consent_granted", "consent_withdrawn"])


@override_settings(BIOMETRIC_CONSENT_REQUIRED=True)
class ConsentEnforcementTests(MediaIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("gated")

    def test_identity_analysis_cannot_be_queued_without_consent(self):
        image, _analysis, _faces = seed_identity_data(self.user, "gated")
        IdentityAnalysis.objects.all().delete()
        with self.assertRaises(ValidationError) as caught:
            enqueue_identity_analysis(image)
        self.assertIn("Biometric consent", str(caught.exception.detail))
        self.assertFalse(IdentityAnalysis.objects.exists())

    def test_with_consent_the_request_gets_past_the_consent_gate(self):
        image, _analysis, _faces = seed_identity_data(self.user, "gated")
        record_consent(self.user, PURPOSE, True, POLICY)
        with patch("identities.services.get_identity_provider") as get_provider:
            get_provider.return_value.healthcheck.side_effect = ValidationError("provider stub")
            with self.assertRaises(ValidationError) as caught:
                enqueue_identity_analysis(image)
        self.assertNotIn("Biometric consent", str(caught.exception.detail))

    def test_consent_withdrawn_before_the_task_runs_fails_the_analysis_safely(self):
        _image, analysis, _faces = seed_identity_data(self.user, "late")
        IdentityFace.objects.all().delete()
        analysis.status = "queued"
        analysis.save(update_fields=["status"])
        result = process_identity_analysis(analysis.pk)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_reason, "Biometric consent is not active.")
        self.assertFalse(IdentityFace.objects.exists())

    @override_settings(IDENTITY_GUARD_ENABLED=True)
    @patch("enhancement.services.apply_identity_guard")
    def test_identity_guard_is_skipped_without_consent(self, guard):
        source = np.full((80, 80, 3), 120, dtype=np.uint8)
        with override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False):
            _out, _level, parameters = enhance_bgr(source, "auto", allow_biometric_processing=False)
        self.assertEqual(parameters["identity_guard"]["status"], "skipped")
        self.assertIn("consent", parameters["identity_guard"]["fallback_reason"])
        guard.assert_not_called()

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_the_enhancement_task_passes_the_consent_decision_to_the_pipeline(self):
        success, encoded = cv2.imencode(".jpg", np.full((64, 64, 3), 120, dtype=np.uint8))
        self.assertTrue(success)
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        image = ImageUpload.objects.create(
            owner=self.user, batch=batch, original_name="c.jpg", file_extension=".jpg",
            mime_type="image/jpeg", file_size=len(encoded.tobytes()), width=64, height=64, status="uploaded",
        )
        image.original_file.save("c.jpg", ContentFile(encoded.tobytes()))
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=image, status="completed", output_format="jpeg",
            output_name="c.jpg", output_width=64, output_height=64,
        )
        preprocessing.processed_file.save("c.jpg", ContentFile(encoded.tobytes()))

        def run():
            result = ImageEnhancementResult.objects.create(
                owner=self.user, image=image, preprocessing_result=preprocessing, requested_level="auto",
            )
            with patch("enhancement.services.enhance_bgr", wraps=enhance_bgr) as pipeline:
                process_enhancement_result(result)
            return pipeline.call_args.kwargs["allow_biometric_processing"]

        self.assertFalse(run())
        record_consent(self.user, PURPOSE, True, POLICY)
        self.assertTrue(run())


class ErasureTests(MediaIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("erased")
        self.other = make_user("bystander")

    def _seed_full(self, owner, tag):
        image, analysis, faces = seed_identity_data(owner, tag)
        group = make_group(owner, f"{tag} person", faces)
        export = make_export(owner, group)
        return image, analysis, faces, group, export

    def test_full_erasure_removes_every_biometric_record_and_export_file(self):
        image, _analysis, _faces, _group, export = self._seed_full(self.user, "mine")
        _o_image, _o_analysis, _o_faces, other_group, other_export = self._seed_full(self.other, "theirs")
        export_name = export.export_file.name
        self.assertTrue(default_storage.exists(export_name))

        result = erase_facial_data(self.user)

        self.assertTrue(result["verified"])
        self.assertEqual(result["deleted"], {
            "analyses": 1, "faces": 2, "memberships": 2, "groups": 1, "changes": 1, "exports": 1,
        })
        self.assertTrue(all(value == 0 for value in result["remaining"].values()))
        self.assertFalse(default_storage.exists(export_name))
        self.assertEqual(facial_data_counts(self.user), result["remaining"])
        # The user's photos are their own content and are not touched here.
        self.assertTrue(ImageUpload.objects.filter(pk=image.pk).exists())
        # Another user's data is untouched.
        self.assertEqual(IdentityFace.objects.filter(owner=self.other).count(), 2)
        self.assertTrue(IdentityGroup.objects.filter(pk=other_group.pk).exists())
        self.assertTrue(default_storage.exists(other_export.export_file.name))

    def test_erasure_is_recorded_for_audit_without_personal_data(self):
        self._seed_full(self.user, "audit")
        result = erase_facial_data(self.user)
        request = PrivacyRequest.objects.get(pk=result["request_id"])
        self.assertEqual(request.user, self.user)
        self.assertEqual(request.kind, "facial_erasure")
        self.assertEqual(request.status, "completed")
        self.assertTrue(request.verified)
        self.assertNotIn("audit person", json.dumps(request.summary))
        self.assertTrue(
            ActivityEvent.objects.filter(user=self.user, event="facial_data_erased").exists()
        )

    def test_a_failed_verification_is_reported_and_recorded(self):
        self._seed_full(self.user, "stuck")
        with patch("privacy.services._stored_export_files", return_value=["left-behind.zip"]):
            result = erase_facial_data(self.user)
        self.assertFalse(result["verified"])
        request = PrivacyRequest.objects.get(pk=result["request_id"])
        self.assertEqual(request.status, "failed")
        self.assertFalse(request.verified)

    def test_deleting_the_account_leaves_no_biometric_data_or_files(self):
        _image, _analysis, _faces, _group, export = self._seed_full(self.user, "gone")
        user_id, export_name = self.user.pk, export.export_file.name
        erase_facial_data(self.user)  # leaves an audit record that must survive
        self._seed_full(self.user, "again")
        self.user.delete()
        self.assertFalse(IdentityAnalysis.objects.filter(owner_id=user_id).exists())
        self.assertFalse(IdentityFace.objects.filter(owner_id=user_id).exists())
        self.assertFalse(IdentityGroup.objects.filter(owner_id=user_id).exists())
        self.assertFalse(IdentityChange.objects.filter(owner_id=user_id).exists())
        self.assertFalse(IdentityGroupExport.objects.filter(owner_id=user_id).exists())
        self.assertFalse(default_storage.exists(export_name))
        try:
            _dirs, files = default_storage.listdir(f"users/{user_id}/identity-exports")
        except (FileNotFoundError, OSError):
            files = []
        self.assertEqual(files, [])
        # The audit trail keeps counts but no longer points at a person.
        self.assertTrue(PrivacyRequest.objects.filter(user__isnull=True, kind="facial_erasure").exists())


@override_settings(FACIAL_DATA_RETENTION_DAYS=30)
class RetentionTests(MediaIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("retained")
        self.now = timezone.now()

    def _old(self, analysis):
        IdentityAnalysis.objects.filter(pk=analysis.pk).update(created_at=self.now - timedelta(days=90))

    def test_purge_removes_only_expired_faces_and_recomputes_groups(self):
        _old_image, old_analysis, old_faces = seed_identity_data(self.user, "old", ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))
        _new_image, _new_analysis, new_faces = seed_identity_data(self.user, "new", ((0.0, 0.0, 1.0),))
        self._old(old_analysis)
        shared = make_group(self.user, "shared", [old_faces[0], new_faces[0]])
        doomed = make_group(self.user, "doomed", [old_faces[1]])

        summary = purge_expired_facial_data(now=self.now)

        self.assertEqual(summary["users_affected"], 1)
        self.assertEqual(summary["deleted"]["faces"], 2)
        self.assertEqual(summary["deleted"]["groups_removed"], 1)
        self.assertEqual(summary["unverified_users"], 0)
        self.assertFalse(IdentityGroup.objects.filter(pk=doomed.pk).exists())
        self.assertFalse(IdentityChange.objects.filter(after__name="doomed").exists())
        shared.refresh_from_db()
        self.assertEqual(shared.member_count, 1)
        centroid = reveal_embedding(shared.protected_centroid, 3)
        np.testing.assert_allclose(centroid, [0.0, 0.0, 1.0], atol=1e-6)
        self.assertEqual(IdentityFace.objects.filter(owner=self.user).count(), 1)
        request = PrivacyRequest.objects.get(kind="retention_purge")
        self.assertIsNone(request.user)
        self.assertTrue(request.verified)

    @override_settings(FACIAL_DATA_RETENTION_DAYS=0)
    def test_retention_disabled_changes_nothing(self):
        _image, analysis, _faces = seed_identity_data(self.user, "kept")
        self._old(analysis)
        self.assertEqual(purge_expired_facial_data(now=self.now), {"enabled": False, "retention_days": 0})
        self.assertEqual(IdentityFace.objects.count(), 2)
        self.assertFalse(PrivacyRequest.objects.exists())

    def test_a_run_with_nothing_expired_writes_no_audit_record(self):
        seed_identity_data(self.user, "fresh")
        summary = purge_expired_facial_data(now=self.now)
        self.assertEqual(summary["users_affected"], 0)
        self.assertFalse(PrivacyRequest.objects.exists())

    def test_command_and_task_run_the_same_purge(self):
        _image, analysis, _faces = seed_identity_data(self.user, "via-task")
        self._old(analysis)
        out = StringIO()
        call_command("purge_expired_facial_data", stdout=out)
        self.assertIn("Purged data for 1 user", out.getvalue())
        self.assertEqual(IdentityFace.objects.count(), 0)
        self.assertEqual(purge_expired_facial_data_task()["users_affected"], 0)


ROTATED = dict(
    IDENTITY_EMBEDDING_KEY="the-rotated-secret-value-0123456789", IDENTITY_EMBEDDING_KEY_ID=2,
    IDENTITY_EMBEDDING_PREVIOUS_KEYS={1: "the-retired-secret-value-0123456789", 9: LEGACY_KEY},
)


class ReencryptionTests(MediaIsolatedTestCase):
    """Rows in every state a real database can hold after years of use."""

    def setUp(self):
        super().setUp()
        self._rotated = override_settings(**ROTATED)
        self._rotated.enable()
        self.user = make_user("rotator")
        _image, _analysis, faces = seed_identity_data(
            self.user, "rot", vectors=((1.0, 0.0, 0.0),) * 4,
        )
        self.faces = faces
        self.vector = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        with override_settings(IDENTITY_EMBEDDING_KEY="the-retired-secret-value-0123456789",
                               IDENTITY_EMBEDDING_KEY_ID=1, IDENTITY_EMBEDDING_PREVIOUS_KEYS={}):
            retired = protect_embedding(self.vector)
        self._set_face(0, LEGACY_PAYLOAD)  # legacy FCID1, readable via the previous-keys list
        self._set_face(1, retired)  # AES under the retired key id
        self._set_face(2, protect_embedding(self.vector))  # already current
        self._set_face(3, b"garbage-that-is-not-a-payload")  # unreadable
        self.group = make_group(self.user, "Rotated", [])
        IdentityGroup.objects.filter(pk=self.group.pk).update(protected_centroid=retired)
        self.blank_group = make_group(self.user, "Blank", [])
        IdentityGroup.objects.filter(pk=self.blank_group.pk).update(protected_centroid=b"")

    def tearDown(self):
        self._rotated.disable()
        super().tearDown()

    def _set_face(self, index, payload):
        IdentityFace.objects.filter(pk=self.faces[index].pk).update(protected_embedding=payload)

    def _payload(self, index):
        return bytes(IdentityFace.objects.get(pk=self.faces[index].pk).protected_embedding)

    def _drop_unreadable_face(self):
        IdentityFace.objects.filter(pk=self.faces[3].pk).delete()

    def test_a_dry_run_reports_the_work_and_changes_nothing(self):
        before = [self._payload(i) for i in range(4)]
        stats = reencrypt_identity_embeddings(dry_run=True)
        self.assertEqual(stats["faces"], {"total": 4, "already_current": 1, "upgraded": 2, "failed": 1, "empty": 0})
        self.assertEqual(stats["groups"], {"total": 1, "already_current": 0, "upgraded": 1, "failed": 0, "empty": 1})
        self.assertEqual([self._payload(i) for i in range(4)], before)
        self.assertFalse(PrivacyRequest.objects.filter(kind="key_rotation").exists())

    def test_rotation_upgrades_every_readable_record_and_preserves_the_vectors(self):
        untouched_current = self._payload(2)
        untouched_garbage = self._payload(3)
        face_updated_at = IdentityFace.objects.get(pk=self.faces[0].pk).updated_at
        group_updated_at = IdentityGroup.objects.get(pk=self.group.pk).updated_at

        stats = reencrypt_identity_embeddings()

        self.assertEqual(stats["faces"]["upgraded"], 2)
        self.assertEqual(stats["groups"]["upgraded"], 1)
        legacy_vector = np.array([0.25, -0.5, 0.75], dtype=np.float32)  # what LEGACY_PAYLOAD encodes
        for index, expected in ((0, legacy_vector), (1, self.vector), (2, self.vector)):
            payload = self._payload(index)
            self.assertTrue(is_current_payload(payload), index)
            np.testing.assert_array_equal(reveal_embedding(payload, 3), expected)
        centroid = bytes(IdentityGroup.objects.get(pk=self.group.pk).protected_centroid)
        self.assertEqual(payload_format(centroid), ("v2", 2))
        np.testing.assert_array_equal(reveal_embedding(centroid, 3), self.vector)
        # Untouched: the already-current record and the unreadable one.
        self.assertEqual(self._payload(2), untouched_current)
        self.assertEqual(self._payload(3), untouched_garbage)
        self.assertEqual(bytes(IdentityGroup.objects.get(pk=self.blank_group.pk).protected_centroid), b"")
        # Rotation must not reorder identity groups (candidates are ordered by updated_at).
        self.assertEqual(IdentityFace.objects.get(pk=self.faces[0].pk).updated_at, face_updated_at)
        self.assertEqual(IdentityGroup.objects.get(pk=self.group.pk).updated_at, group_updated_at)

    def test_a_failure_is_recorded_for_audit_and_the_run_is_idempotent(self):
        first = reencrypt_identity_embeddings()
        self.assertEqual(first["failed"], 1)
        record = PrivacyRequest.objects.get(kind="key_rotation")
        self.assertEqual(record.status, "failed")
        self.assertFalse(record.verified)
        self.assertIsNone(record.user)
        second = reencrypt_identity_embeddings()
        self.assertEqual(second["faces"]["upgraded"], 0)
        self.assertEqual(second["groups"]["upgraded"], 0)
        self.assertEqual(PrivacyRequest.objects.filter(kind="key_rotation").count(), 2)

    def test_batching_gives_the_same_result(self):
        stats = reencrypt_identity_embeddings(batch_size=1)
        self.assertEqual(stats["faces"]["upgraded"], 2)
        self.assertEqual(stats["faces"]["total"], 4)
        self.assertEqual(stats["groups"]["upgraded"], 1)

    def test_a_clean_database_completes_and_the_audit_report_shows_it(self):
        self._drop_unreadable_face()
        before = embedding_encryption_status()
        self.assertTrue(before["rotation_needed"])
        self.assertEqual((before["legacy_v1"], before["older_key"]), (1, 2))

        stats = reencrypt_identity_embeddings()

        self.assertEqual(stats["failed"], 0)
        record = PrivacyRequest.objects.get(kind="key_rotation")
        self.assertEqual((record.status, record.verified), ("completed", True))
        after = embedding_encryption_status()
        self.assertFalse(after["rotation_needed"])
        self.assertEqual(after["current_key"], 4)
        encryption = audit_report()["encryption"]
        self.assertEqual(encryption["current_key_id"], 2)
        self.assertEqual(encryption["previous_keys_configured"], 2)
        self.assertFalse(encryption["stored_records"]["rotation_needed"])

    def test_a_second_run_on_a_clean_database_writes_nothing(self):
        self._drop_unreadable_face()
        reencrypt_identity_embeddings()
        reencrypt_identity_embeddings()
        self.assertEqual(PrivacyRequest.objects.filter(kind="key_rotation").count(), 1)

    def test_the_command_reports_and_fails_loudly_on_unreadable_records(self):
        out = StringIO()
        with self.assertRaisesMessage(CommandError, "could not be decrypted"):
            call_command("reencrypt_identity_embeddings", "--dry-run", stdout=out)
        self.assertIn("2 would be upgraded", out.getvalue())
        self._drop_unreadable_face()
        clean = StringIO()
        call_command("reencrypt_identity_embeddings", stdout=clean)
        self.assertIn("All stored embeddings use the current key.", clean.getvalue())

    def test_the_command_rejects_a_bad_batch_size(self):
        with self.assertRaises(CommandError):
            call_command("reencrypt_identity_embeddings", "--batch-size=0")


class ExportTests(MediaIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("exporter")
        self.other = make_user("someone-else")

    def test_export_contains_the_users_data_and_no_biometric_templates(self):
        image, _analysis, faces = seed_identity_data(self.user, "portrait")
        make_group(self.user, "Aunt Sara", faces)
        record_consent(self.user, PURPOSE, True, POLICY)
        seed_identity_data(self.other, "not-mine")

        export = build_privacy_export(self.user)
        text = json.dumps(export, default=str)

        self.assertEqual(export["account"]["email"], "exporter@example.com")
        self.assertEqual([row["original_name"] for row in export["images"]], ["portrait.jpg"])
        self.assertEqual(export["identity"]["groups"][0]["display_name"], "Aunt Sara")
        self.assertEqual(export["identity"]["face_records"], 2)
        self.assertTrue(export["consents"][0]["granted"])
        self.assertNotIn("not-mine", text)
        self.assertNotIn("embedding", text.replace("Face embeddings and group centroids", ""))
        self.assertNotIn("centroid", text.replace("Face embeddings and group centroids", ""))
        self.assertNotIn("password", text)
        self.assertEqual(image.owner_id, self.user.pk)

    @override_settings(PRIVACY_EXPORT_MAX_ROWS=1)
    def test_large_collections_are_capped_and_flagged(self):
        for index in range(3):
            batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
            ImageUpload.objects.create(owner=self.user, batch=batch, original_name=f"{index}.jpg", status="uploaded")
        export = build_privacy_export(self.user)
        self.assertEqual(len(export["images"]), 1)
        self.assertTrue(export["truncated"]["images"])
        self.assertFalse(export["truncated"]["consents"])


class AuditReportTests(MediaIsolatedTestCase):
    def test_report_summarises_consent_and_requests_without_personal_data(self):
        granted, withdrawn, stale = make_user("granted"), make_user("withdrawn"), make_user("stale")
        record_consent(granted, PURPOSE, True, POLICY)
        record_consent(withdrawn, PURPOSE, True, POLICY)
        record_consent(withdrawn, PURPOSE, False, "")
        BiometricConsent.objects.create(user=stale, purpose=PURPOSE, granted=True, policy_version="old")
        PrivacyRequest.objects.create(user=None, kind="facial_erasure", status="failed", verified=False)

        report = audit_report()
        consent = report["consent"]

        self.assertEqual(consent["users_with_active_consent"], 1)
        self.assertEqual(consent["users_with_withdrawn_consent"], 1)
        self.assertEqual(consent["users_needing_reconsent"], 1)
        self.assertEqual(report["unverified_erasures_total"], 1)
        self.assertEqual(report["requests_last_30_days"]["facial_erasure"]["failed"], 1)
        self.assertFalse(report["encryption"]["uploaded_images_encrypted_at_rest"])
        self.assertNotIn("@example.com", json.dumps(report, default=str))


class PrivacyApiTests(APITestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(
            MEDIA_ROOT=self._media.name, IDENTITY_EMBEDDING_KEY="privacy-test-key",
            BIOMETRIC_CONSENT_POLICY_VERSION=POLICY,
        )
        self._override.enable()
        self.user = make_user("apiuser")
        self.authenticate(self.user)

    def tearDown(self):
        self._override.disable()
        self._media.cleanup()

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_every_endpoint_requires_authentication(self):
        self.client.credentials()
        for name in ("consents", "export", "facial-data", "audit"):
            self.assertEqual(self.client.get(reverse(f"privacy:{name}")).status_code, status.HTTP_401_UNAUTHORIZED, name)

    def test_consent_can_be_read_granted_and_withdrawn(self):
        url = reverse("privacy:consents")
        initial = self.client.get(url).data
        self.assertEqual(initial["current_policy_version"], POLICY)
        self.assertFalse(initial["purposes"][0]["granted"])

        granted = self.client.post(url, {"purpose": "face_identity", "granted": True, "policy_version": POLICY}, format="json")
        self.assertEqual(granted.status_code, status.HTTP_200_OK)
        self.assertTrue(granted.data["purposes"][0]["active"])

        bad = self.client.post(url, {"purpose": "face_identity", "granted": True, "policy_version": "wrong"}, format="json")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

        withdrawn = self.client.post(url, {"purpose": "face_identity", "granted": False}, format="json")
        self.assertFalse(withdrawn.data["purposes"][0]["active"])
        self.assertNotIn("erasure", withdrawn.data)

    def test_withdrawing_with_erasure_deletes_the_facial_data(self):
        _image, _analysis, faces = seed_identity_data(self.user, "api")
        make_group(self.user, "Friend", faces)
        response = self.client.post(
            reverse("privacy:consents"),
            {"purpose": "face_identity", "granted": False, "erase_facial_data": True}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["erasure"]["verified"])
        self.assertEqual(IdentityFace.objects.filter(owner=self.user).count(), 0)
        self.assertEqual(
            PrivacyRequest.objects.get(pk=response.data["erasure"]["request_id"]).trigger,
            "consent_withdrawal",
        )

    def test_erasing_while_granting_is_rejected(self):
        response = self.client.post(
            reverse("privacy:consents"),
            {"purpose": "face_identity", "granted": True, "policy_version": POLICY, "erase_facial_data": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_facial_data_can_be_inspected_and_erased_only_with_confirmation(self):
        seed_identity_data(self.user, "inspect")
        url = reverse("privacy:facial-data")
        self.assertEqual(self.client.get(url).data["stored"]["faces"], 2)
        self.assertEqual(self.client.delete(url, {}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.delete(url, {"confirm": False}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(IdentityFace.objects.filter(owner=self.user).count(), 2)
        response = self.client.delete(url, {"confirm": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["verified"])
        self.assertEqual(self.client.get(url).data["stored"]["faces"], 0)

    def test_users_can_only_erase_their_own_data(self):
        other = make_user("neighbour")
        seed_identity_data(other, "neighbour")
        self.client.delete(reverse("privacy:facial-data"), {"confirm": True}, format="json")
        self.assertEqual(IdentityFace.objects.filter(owner=other).count(), 2)

    def test_export_is_a_json_attachment_and_is_recorded(self):
        response = self.client.get(reverse("privacy:export"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response.data["account"]["username"], "apiuser")
        self.assertTrue(PrivacyRequest.objects.filter(user=self.user, kind="export").exists())

    def test_the_audit_report_is_administrator_only(self):
        url = reverse("privacy:audit")
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)
        admin = make_user("auditor", role="admin")
        self.authenticate(admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("consent", response.data)


class PrivacyChecksTests(SimpleTestCase):
    @override_settings(IDENTITY_EMBEDDING_KEY_ID=0)
    def test_the_key_id_must_fit_in_one_byte(self):
        self.assertIn("privacy.E006", [issue.id for issue in privacy_configuration_check(None)])

    @override_settings(IDENTITY_EMBEDDING_KEY="a" * 40, IDENTITY_EMBEDDING_KEY_ID=1,
                       IDENTITY_EMBEDDING_PREVIOUS_KEYS={1: "b" * 40})
    def test_a_previous_key_cannot_reuse_the_current_id_for_another_secret(self):
        self.assertIn("privacy.E007", [issue.id for issue in privacy_configuration_check(None)])

    @override_settings(SECRET_KEY="s" * 40, IDENTITY_EMBEDDING_KEY="k" * 40, IDENTITY_EMBEDDING_KEY_ID=2,
                       IDENTITY_EMBEDDING_PREVIOUS_KEYS={1: "old" * 12}, BIOMETRIC_CONSENT_REQUIRED=True,
                       FACIAL_DATA_RETENTION_DAYS=30)
    def test_leftover_retired_keys_are_flagged_at_deployment(self):
        self.assertEqual([issue.id for issue in privacy_deployment_check(None)], ["privacy.W005"])
        self.assertEqual(privacy_configuration_check(None), [])

    @override_settings(SECRET_KEY="s" * 40, IDENTITY_EMBEDDING_KEY="s" * 40)
    def test_embedding_key_must_not_fall_back_to_the_django_secret(self):
        self.assertIn("privacy.E001", [issue.id for issue in privacy_deployment_check(None)])

    @override_settings(SECRET_KEY="s" * 40, IDENTITY_EMBEDDING_KEY="  ")
    def test_a_blank_embedding_key_is_an_error(self):
        self.assertIn("privacy.E005", [issue.id for issue in privacy_deployment_check(None)])

    def test_the_real_settings_never_turn_a_blank_key_into_an_empty_one(self):
        code = (
            "import os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
            "from django.conf import settings; "
            "print(repr(settings.IDENTITY_EMBEDDING_KEY == settings.SECRET_KEY), "
            "repr(bool(settings.IDENTITY_EMBEDDING_KEY)))"
        )
        environment = {**os.environ, "IDENTITY_EMBEDDING_KEY": "   "}
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR, env=environment,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip().splitlines()[-1], "True True")

    @override_settings(
        SECRET_KEY="s" * 40, IDENTITY_EMBEDDING_KEY="k" * 40,
        BIOMETRIC_CONSENT_REQUIRED=True, FACIAL_DATA_RETENTION_DAYS=365,
    )
    def test_a_fully_configured_deployment_has_no_findings(self):
        self.assertEqual(privacy_deployment_check(None), [])

    @override_settings(
        SECRET_KEY="s" * 40, IDENTITY_EMBEDDING_KEY="short-key",
        BIOMETRIC_CONSENT_REQUIRED=False, FACIAL_DATA_RETENTION_DAYS=0,
    )
    def test_weak_or_missing_controls_are_warned_about(self):
        ids = {issue.id for issue in privacy_deployment_check(None)}
        self.assertEqual(ids, {"privacy.W001", "privacy.W002", "privacy.W003"})

    @override_settings(FACIAL_DATA_RETENTION_DAYS=-1, BIOMETRIC_CONSENT_POLICY_VERSION=" ", PRIVACY_EXPORT_MAX_ROWS=0)
    def test_invalid_privacy_settings_are_errors(self):
        ids = {issue.id for issue in privacy_configuration_check(None)}
        self.assertEqual(ids, {"privacy.E002", "privacy.E003", "privacy.E004"})

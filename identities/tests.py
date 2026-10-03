import math
from unittest.mock import MagicMock, patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult

from .management import (delete_empty_group, merge_groups, move_membership,
                         remove_membership, rename_group, set_profile_membership)
from .models import (IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup,
                     IdentityMembership)
from .providers import IdentityEmbeddingProvider, SFaceOnnxProvider
from .serializers import IdentityGroupSerializer
from .services import _choose_group, _refresh_group, process_identity_analysis
from .storage import protect_embedding, reveal_embedding
from privacy.testing import grant_face_consent

User = get_user_model()


@override_settings(IDENTITY_EMBEDDING_KEY="test-only-identity-key")
class ProtectedEmbeddingTests(TestCase):
    def test_embedding_round_trip_is_not_plain_float_storage(self):
        vector = np.array([0.2, -0.3, 0.9327379], dtype=np.float32)
        protected = protect_embedding(vector)
        self.assertNotIn(vector.tobytes(), protected)
        np.testing.assert_allclose(reveal_embedding(protected, 3), vector)

    def test_tampering_fails_without_disclosing_payload(self):
        protected = bytearray(protect_embedding([1.0, 0.0]))
        protected[-1] ^= 1
        with self.assertRaisesMessage(Exception, "protected identity embedding is invalid"):
            reveal_embedding(protected, 2)


class ProviderContractTests(TestCase):
    class Provider(IdentityEmbeddingProvider):
        name = "test"
        dimension = 3

        def healthcheck(self):
            return True

        def embed(self, prepared_face):
            return [3.0, 4.0, 0.0]

    def test_provider_normalizes_and_compares_vectors(self):
        provider = self.Provider()
        vector = provider.validate_embedding(provider.embed(None))
        np.testing.assert_allclose(vector, [0.6, 0.8, 0.0])
        self.assertAlmostEqual(provider.similarity(vector, vector), 1.0)

    def test_provider_rejects_nan_and_wrong_dimensions(self):
        provider = self.Provider()
        for invalid in ([1, 2], [1, float("nan"), 2]):
            with self.assertRaisesMessage(Exception, "invalid embedding"):
                provider.validate_embedding(invalid)


class InstalledSFaceProviderTests(TestCase):
    def test_verified_model_extracts_normalized_feature_from_yunet_geometry(self):
        from pathlib import Path

        from django.conf import settings

        if not (Path(settings.IDENTITY_MODEL_PATH).is_file() and settings.IDENTITY_LICENSE_RECORD_PATH
                and Path(settings.IDENTITY_LICENSE_RECORD_PATH).is_file()):
            self.skipTest("The SFace model and its licence record are not installed here.")
        provider = SFaceOnnxProvider()
        self.assertTrue(provider.healthcheck())
        image = np.full((320, 320, 3), 127, dtype=np.uint8)
        box = {"x": 60, "y": 50, "width": 200, "height": 220}
        landmarks = [
            {"x": 120, "y": 125}, {"x": 200, "y": 125},
            {"x": 160, "y": 170}, {"x": 125, "y": 220},
            {"x": 195, "y": 220},
        ]
        aligned = provider.prepare_face(image, box, landmarks)
        feature = provider.embed(aligned)
        self.assertEqual(aligned.shape, (112, 112, 3))
        self.assertEqual(feature.shape, (settings.IDENTITY_EMBEDDING_DIMENSION,))
        self.assertTrue(np.isfinite(feature).all())
        self.assertAlmostEqual(float(np.linalg.norm(feature)), 1.0, places=5)


class IdentityPrivacyApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="identity-owner", email="identity-owner@example.com", password="Password!123", is_email_verified=True)
        self.other = User.objects.create_user(username="identity-other", email="identity-other@example.com", password="Password!123", is_email_verified=True)
        self.group = IdentityGroup.objects.create(owner=self.owner, display_name="Person 001", embedding_dimension=3, provider="test", policy_version="test-v1", protected_centroid=b"private-centroid")

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_group_list_is_owner_scoped_and_hides_private_fields(self):
        self.authenticate(self.owner)
        response = self.client.get(reverse("identities:group-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data["results"][0] if isinstance(response.data, dict) else response.data[0]
        self.assertEqual(row["display_name"], "Person 001")
        self.assertNotIn("protected_centroid", row)
        self.assertNotIn("model_checksum", row)

    def test_other_owner_group_identifier_returns_404(self):
        self.authenticate(self.other)
        response = self.client.get(reverse("identities:group-detail", args=[self.group.pk]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_serializer_has_no_embedding_or_centroid_field(self):
        self.assertNotIn("protected_centroid", IdentityGroupSerializer(self.group).data)


@override_settings(IDENTITY_EMBEDDING_KEY="management-test-key")
class IdentityGroupManagementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="manager", email="manager@example.com", password="Password!123")
        self.other = User.objects.create_user(username="outsider", email="outsider@example.com", password="Password!123")
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="group.jpg", status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}])
        analysis = IdentityAnalysis.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, face_validation=validation, status="completed", provider="test", policy_version="test-v1")
        self.face = IdentityFace.objects.create(owner=self.owner, image=image, analysis=analysis, face_index=0, protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test")
        self.source = IdentityGroup.objects.create(owner=self.owner, display_name="Person 001", member_count=1, protected_centroid=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test", policy_version="test-v1")
        self.target = IdentityGroup.objects.create(owner=self.owner, display_name="Person 002", embedding_dimension=3, provider="test", policy_version="test-v1")
        self.membership = IdentityMembership.objects.create(owner=self.owner, identity_face=self.face, identity_group=self.source, state="new_group", decision_method="test")

    def test_rename_move_profile_remove_and_delete_are_audited(self):
        rename_group(self.source, self.owner, "Family")
        moved = move_membership(self.membership, self.target, self.owner, "correction")
        set_profile_membership(self.target, moved, self.owner)
        remove_membership(moved, self.owner, "incorrect face")
        delete_empty_group(self.source, self.owner, "unused")
        self.target.refresh_from_db()
        self.assertIsNone(self.target.profile_membership_id)
        self.assertEqual(set(IdentityChange.objects.filter(owner=self.owner).values_list("action", flat=True)), {"renamed", "moved", "profile_set", "removed", "deleted"})

    def test_merge_moves_members_and_marks_source(self):
        source, target = merge_groups(self.source, self.target, self.owner)
        source.refresh_from_db()
        target.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(source.state, "merged")
        self.assertEqual(self.membership.identity_group_id, target.pk)
        self.assertEqual(target.member_count, 1)

    def test_nonempty_group_cannot_be_deleted(self):
        with self.assertRaisesMessage(Exception, "Remove or move"):
            delete_empty_group(self.source, self.owner)

    def test_cross_owner_management_is_rejected(self):
        with self.assertRaises(IdentityGroup.DoesNotExist):
            rename_group(self.source, self.other, "Stolen")


class IdentityGroupManagementApiTests(APITestCase):
    """Endpoint-level coverage for every Feature 19 write operation.

    IdentityGroupManagementTests above covers the service layer directly;
    these tests drive the same operations through the authenticated HTTP
    routes so that URL wiring, serializers and owner-scope enforcement at
    the view layer are also verified.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="api-owner", email="api-owner@example.com", password="Password!123", is_email_verified=True)
        self.other = User.objects.create_user(username="api-other", email="api-other@example.com", password="Password!123", is_email_verified=True)
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="api.jpg", status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}])
        analysis = IdentityAnalysis.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, face_validation=validation, status="completed", provider="test", policy_version="test-v1")
        self.face = IdentityFace.objects.create(owner=self.owner, image=image, analysis=analysis, face_index=0, protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test")
        self.source = IdentityGroup.objects.create(owner=self.owner, display_name="Person 001", member_count=1, protected_centroid=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test", policy_version="test-v1")
        self.target = IdentityGroup.objects.create(owner=self.owner, display_name="Person 002", embedding_dimension=3, provider="test", policy_version="test-v1")
        self.membership = IdentityMembership.objects.create(owner=self.owner, identity_face=self.face, identity_group=self.source, state="new_group", decision_method="test")

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    # Rename
    def test_rename_endpoint_updates_display_name_and_audits(self):
        self.authenticate(self.owner)
        response = self.client.patch(reverse("identities:group-rename", args=[self.source.pk]), {"display_name": "Family"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["display_name"], "Family")
        self.assertTrue(IdentityChange.objects.filter(owner=self.owner, action="renamed").exists())

    def test_rename_endpoint_rejects_other_owner(self):
        self.authenticate(self.other)
        response = self.client.patch(reverse("identities:group-rename", args=[self.source.pk]), {"display_name": "Stolen"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rename_endpoint_rejects_invalid_payload(self):
        self.authenticate(self.owner)
        response = self.client.patch(reverse("identities:group-rename", args=[self.source.pk]), {"display_name": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # Merge
    def test_merge_endpoint_moves_membership_into_target(self):
        self.authenticate(self.owner)
        response = self.client.post(reverse("identities:group-merge", args=[self.target.pk]), {"source_group_id": self.source.pk, "reason": "duplicate"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.identity_group_id, self.target.pk)

    def test_merge_endpoint_rejects_other_owner_target(self):
        self.authenticate(self.other)
        response = self.client.post(reverse("identities:group-merge", args=[self.target.pk]), {"source_group_id": self.source.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Move
    def test_move_endpoint_moves_membership_to_target_group(self):
        self.authenticate(self.owner)
        response = self.client.patch(reverse("identities:membership-move", args=[self.membership.pk]), {"target_group_id": self.target.pk, "reason": "correction"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.identity_group_id, self.target.pk)

    def test_move_endpoint_rejects_other_owner(self):
        self.authenticate(self.other)
        response = self.client.patch(reverse("identities:membership-move", args=[self.membership.pk]), {"target_group_id": self.target.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Remove
    def test_remove_endpoint_marks_membership_removed(self):
        self.authenticate(self.owner)
        response = self.client.patch(reverse("identities:membership-remove", args=[self.membership.pk]), {"reason": "incorrect face"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.state, IdentityMembership.State.REMOVED)

    def test_remove_endpoint_rejects_other_owner(self):
        self.authenticate(self.other)
        response = self.client.patch(reverse("identities:membership-remove", args=[self.membership.pk]), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Set profile
    def test_set_profile_endpoint_updates_group_profile(self):
        self.authenticate(self.owner)
        response = self.client.patch(reverse("identities:group-profile", args=[self.source.pk]), {"membership_id": self.membership.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.source.refresh_from_db()
        self.assertEqual(self.source.profile_membership_id, self.membership.pk)

    def test_set_profile_endpoint_rejects_other_owner(self):
        self.authenticate(self.other)
        response = self.client.patch(reverse("identities:group-profile", args=[self.source.pk]), {"membership_id": self.membership.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Delete
    def test_delete_endpoint_removes_empty_group(self):
        self.authenticate(self.owner)
        response = self.client.delete(reverse("identities:group-delete", args=[self.target.pk]), {"reason": "unused"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(IdentityGroup.objects.filter(pk=self.target.pk).exists())

    def test_delete_endpoint_rejects_nonempty_group(self):
        self.authenticate(self.owner)
        response = self.client.delete(reverse("identities:group-delete", args=[self.source.pk]), {"reason": "unused"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(IdentityGroup.objects.filter(pk=self.source.pk).exists())

    def test_delete_endpoint_rejects_other_owner(self):
        self.authenticate(self.other)
        response = self.client.delete(reverse("identities:group-delete", args=[self.target.pk]), {"reason": "unused"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChooseGroupDecisionTests(TestCase):
    """Unit coverage for the new/match/review/owner-isolation decision boundaries.

    Uses synthetic 2D-on-a-circle vectors so exact cosine similarities are
    known in advance; no real images, models or datasets are needed.
    """

    class Provider(IdentityEmbeddingProvider):
        name = "test"
        dimension = 3

        def healthcheck(self):
            return True

        def embed(self, prepared_face):
            return prepared_face

    def setUp(self):
        self.owner = User.objects.create_user(username="chooser", email="chooser@example.com", password="Password!123")
        self.other = User.objects.create_user(username="chooser-other", email="chooser-other@example.com", password="Password!123")
        self.provider = self.Provider()

    @staticmethod
    def _vector(angle_degrees):
        radians = math.radians(angle_degrees)
        return np.array([math.cos(radians), math.sin(radians), 0.0], dtype=np.float32)

    def _make_group(self, owner, angle_degrees):
        return IdentityGroup.objects.create(
            owner=owner, display_name=f"Group {angle_degrees} deg for {owner.pk}",
            protected_centroid=protect_embedding(self._vector(angle_degrees)),
            embedding_dimension=3, provider=self.provider.name, policy_version="test-v1",
        )

    def test_new_decision_when_no_candidate_groups_exist(self):
        decision, group, best, second = _choose_group(self.owner, self.provider, self._vector(0))
        self.assertEqual(decision, "new")
        self.assertIsNone(group)

    def test_new_decision_when_best_candidate_is_below_new_group_threshold(self):
        self._make_group(self.owner, angle_degrees=90)  # cosine similarity ~= 0.0
        decision, group, best, second = _choose_group(self.owner, self.provider, self._vector(0))
        self.assertEqual(decision, "new")

    def test_match_decision_for_a_clearly_closest_candidate(self):
        target = self._make_group(self.owner, angle_degrees=0)   # similarity ~= 1.0
        self._make_group(self.owner, angle_degrees=90)           # similarity ~= 0.0, not ambiguous
        decision, group, best, second = _choose_group(self.owner, self.provider, self._vector(0))
        self.assertEqual(decision, "match")
        self.assertEqual(group.pk, target.pk)
        self.assertGreaterEqual(best, 0.82)

    def test_review_decision_when_two_candidates_are_close_and_above_threshold(self):
        self._make_group(self.owner, angle_degrees=5)  # cos(5 deg) ~= 0.9962
        self._make_group(self.owner, angle_degrees=8)  # cos(8 deg) ~= 0.9903
        decision, group, best, second = _choose_group(self.owner, self.provider, self._vector(0))
        self.assertEqual(decision, "review")
        self.assertGreaterEqual(best, 0.82)
        self.assertLess(best - second, 0.06)

    def test_candidates_are_scoped_to_owner(self):
        self._make_group(self.other, angle_degrees=0)  # a perfect match, but owned by someone else
        decision, group, best, second = _choose_group(self.owner, self.provider, self._vector(0))
        self.assertEqual(decision, "new")


class RefreshGroupCentroidTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="refresher", email="refresher@example.com", password="Password!123")
        batch = UploadBatch.objects.create(owner=self.owner, upload_type="single")
        image = ImageUpload.objects.create(owner=self.owner, batch=batch, original_name="refresh.jpg", status="uploaded")
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1, faces=[{"x": 0, "y": 0, "width": 10, "height": 10}])
        self.analysis = IdentityAnalysis.objects.create(owner=self.owner, image=image, preprocessing_result=preprocessing, face_validation=validation, status="completed", provider="test", policy_version="test-v1")
        self.group = IdentityGroup.objects.create(owner=self.owner, display_name="Person 001", embedding_dimension=3, provider="test", policy_version="test-v1")

    def _face(self, vector, index):
        return IdentityFace.objects.create(owner=self.owner, image=self.analysis.image, analysis=self.analysis, face_index=index, protected_embedding=protect_embedding(vector), embedding_dimension=3, provider="test")

    def test_refresh_computes_normalized_mean_centroid_from_confirmed_members(self):
        IdentityMembership.objects.create(owner=self.owner, identity_face=self._face([1.0, 0.0, 0.0], 0), identity_group=self.group, state=IdentityMembership.State.NEW_GROUP, decision_method="test")
        IdentityMembership.objects.create(owner=self.owner, identity_face=self._face([0.0, 1.0, 0.0], 1), identity_group=self.group, state=IdentityMembership.State.AUTO_MATCHED, decision_method="test")
        _refresh_group(self.group)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 2)
        centroid = reveal_embedding(self.group.protected_centroid, 3)
        np.testing.assert_allclose(centroid, [1 / math.sqrt(2), 1 / math.sqrt(2), 0.0], atol=1e-5)

    def test_refresh_clears_centroid_when_no_confirmed_members_remain(self):
        IdentityMembership.objects.create(owner=self.owner, identity_face=self._face([1.0, 0.0, 0.0], 0), identity_group=self.group, state=IdentityMembership.State.REJECTED, decision_method="test")
        _refresh_group(self.group)
        self.group.refresh_from_db()
        self.assertEqual(self.group.member_count, 0)
        self.assertEqual(self.group.protected_centroid, b"")


class ProcessIdentityAnalysisCancellationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="capture", email="capture@example.com", password="Password!123")
        self.batch = UploadBatch.objects.create(owner=self.owner, upload_type="multiple")
        self.image = ImageUpload.objects.create(owner=self.owner, batch=self.batch, original_name="group.jpg", status="uploaded")
        self.preprocessing = ImagePreprocessingResult.objects.create(owner=self.owner, image=self.image, status="completed")
        self.validation = FaceValidationResult.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.preprocessing,
            decision="accepted", face_count=2,
            faces=[
                {"x": 0, "y": 0, "width": 10, "height": 10},
                {"x": 20, "y": 20, "width": 10, "height": 10},
            ],
        )
        self.analysis = IdentityAnalysis.objects.create(
            owner=self.owner, image=self.image, preprocessing_result=self.preprocessing,
            face_validation=self.validation, status="pending", provider="test", policy_version="test-v1",
        )
        # Facial processing sits behind recorded consent, and production
        # requires it, so grant it here rather than relying on the flag
        # being switched off on this machine.
        grant_face_consent(self.owner)

    def test_cancellation_requested_between_faces_stops_before_the_next_embed_call(self):
        batch_pk = self.batch.pk

        def embed_side_effect(crop):
            # Simulate the batch being cancelled while the first face is
            # being embedded, so the second face must not be processed.
            UploadBatch.objects.filter(pk=batch_pk).update(cancellation_requested=True)
            return np.array([1.0, 0.0, 0.0], dtype=np.float32)

        provider = MagicMock()
        provider.name = "test"
        provider.dimension = 3
        provider.model_checksum = "stub"
        provider.healthcheck.return_value = None
        provider.prepare_face.side_effect = lambda image, box, landmarks: image
        provider.embed.side_effect = embed_side_effect
        provider.validate_embedding.side_effect = lambda vector: vector

        with (
            patch("identities.services.get_identity_provider", return_value=provider),
            patch("identities.services._read_image", return_value=np.zeros((4, 4, 3), dtype=np.uint8)),
        ):
            result = process_identity_analysis(self.analysis.pk)

        self.assertEqual(result.status, IdentityAnalysis.Status.CANCELLED)
        self.assertEqual(provider.embed.call_count, 1)
        self.assertEqual(IdentityFace.objects.filter(analysis_id=self.analysis.pk).count(), 0)

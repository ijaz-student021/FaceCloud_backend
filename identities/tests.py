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
from .storage import protect_embedding, reveal_embedding

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
        from django.conf import settings

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

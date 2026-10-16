"""Feature 50: face-based visual search (search the owner's own photos by photo).

Reuses ``_choose_group``'s similarity function/threshold (not a new algorithm), needs
the same biometric consent identity analysis already requires, and the query
embedding must never reach persistent storage or the response body -- only the
resulting similarity floats may.
"""
import json
from unittest.mock import MagicMock, patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from uploads.models import ImageUpload, UploadBatch

from .models import IdentityFace, IdentityGroup, IdentityMembership
from .services import search_by_face
from .storage import protect_embedding
from .test_idempotency import make_provider
from privacy.testing import grant_face_consent, withdraw_face_consent

User = get_user_model()


def query_file():
    """A fresh, genuinely decodable upload each time (the detector itself is mocked,
    but cv2.imdecode is real, and the service reads and exhausts the file object)."""
    import cv2

    ok, encoded = cv2.imencode(".jpg", np.zeros((40, 40, 3), dtype=np.uint8))
    assert ok
    return SimpleUploadedFile("query.jpg", encoded.tobytes(), content_type="image/jpeg")


def fake_detector(boxes, landmarks):
    detector = MagicMock()
    detector.detect.return_value = MagicMock(boxes=boxes, landmarks=landmarks)
    return detector


NO_FACE_DETECTOR = fake_detector([], [])
ONE_FACE_DETECTOR = fake_detector([(0, 0, 40, 40)], [[(1, 1)] * 5])


class VisualSearchTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="search-owner", email="search-owner@example.com", password="Password!123", is_email_verified=True)
        self.other = User.objects.create_user(username="search-other", email="search-other@example.com", password="Password!123", is_email_verified=True)
        self.client = APIClient()
        self.token(self.owner)
        # Facial processing sits behind recorded consent, and production
        # requires it, so grant it here rather than relying on the flag
        # being switched off on this machine.
        grant_face_consent(self.owner, self.other)

    def token(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def make_group(self, owner, vector, name="Person 001", state=IdentityGroup.State.ACTIVE, member_count=1):
        return IdentityGroup.objects.create(
            owner=owner, display_name=name, state=state, member_count=member_count,
            protected_centroid=protect_embedding(vector), embedding_dimension=len(vector),
            provider="test", policy_version="test-v1",
        )

    def add_photo_to_group(self, group, name="member.jpg"):
        batch = UploadBatch.objects.create(owner=group.owner, upload_type="single")
        image = ImageUpload.objects.create(owner=group.owner, batch=batch, original_name=name, status="uploaded")
        face = IdentityFace.objects.create(
            owner=group.owner, image=image, analysis=self._analysis(image, group.owner),
            face_index=0, protected_embedding=protect_embedding([1.0, 0.0, 0.0]), embedding_dimension=3, provider="test",
        )
        IdentityMembership.objects.create(
            owner=group.owner, identity_face=face, identity_group=group, state=IdentityMembership.State.CONFIRMED,
        )
        return image

    def _analysis(self, image, owner):
        from uploads.models import ImagePreprocessingResult
        from validation.models import FaceValidationResult

        from .models import IdentityAnalysis

        preprocessing = ImagePreprocessingResult.objects.create(owner=owner, image=image, status="completed")
        validation = FaceValidationResult.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, decision="accepted", face_count=1,
        )
        return IdentityAnalysis.objects.create(
            owner=owner, image=image, preprocessing_result=preprocessing, face_validation=validation,
            status="completed", provider="test", policy_version="test-v1",
        )

    def search(self, provider=None, detector=None):
        with (
            patch("identities.services.get_identity_provider", return_value=provider or make_provider()),
            patch("identities.services.get_face_detector", return_value=detector or ONE_FACE_DETECTOR),
        ):
            return search_by_face(self.owner, query_file())


class SearchByFaceServiceTests(VisualSearchTestCase):
    def test_no_face_in_the_query_photo_is_reported_distinctly_from_no_match(self):
        result = self.search(detector=NO_FACE_DETECTOR)
        self.assertEqual(result, {"faces_detected": False, "matches": []})

    def test_a_face_with_no_matching_group_returns_an_empty_match_list(self):
        result = self.search()
        self.assertEqual(result, {"faces_detected": True, "matches": []})

    def test_a_confident_match_is_returned_with_its_similarity(self):
        self.make_group(self.owner, [1.0, 0.0, 0.0])
        result = self.search(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        self.assertTrue(result["faces_detected"])
        self.assertEqual(len(result["matches"]), 1)
        self.assertAlmostEqual(result["matches"][0]["similarity"], 1.0, places=4)

    def test_only_groups_at_or_above_the_match_threshold_are_returned(self):
        # cos(30deg) ~= 0.866, above the 0.82 default threshold
        near = self.make_group(self.owner, [np.cos(np.radians(30)), np.sin(np.radians(30)), 0.0], name="Near")
        # cos(60deg) = 0.5, below it
        far = self.make_group(self.owner, [np.cos(np.radians(60)), np.sin(np.radians(60)), 0.0], name="Far")
        result = self.search(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        group_ids = {match["group"].pk for match in result["matches"]}
        self.assertIn(near.pk, group_ids)
        self.assertNotIn(far.pk, group_ids)

    def test_matches_are_ordered_best_first(self):
        worse = self.make_group(self.owner, [np.cos(np.radians(15)), np.sin(np.radians(15)), 0.0], name="Worse")
        better = self.make_group(self.owner, [1.0, 0.0, 0.0], name="Better")
        result = self.search(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        self.assertEqual([match["group"].pk for match in result["matches"]], [better.pk, worse.pk])

    def test_another_users_matching_group_is_never_returned(self):
        self.make_group(self.other, [1.0, 0.0, 0.0])
        result = self.search(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        self.assertEqual(result["matches"], [])

    def test_an_archived_or_merged_group_is_never_returned(self):
        self.make_group(self.owner, [1.0, 0.0, 0.0], name="Archived", state=IdentityGroup.State.ARCHIVED)
        self.make_group(self.owner, [1.0, 0.0, 0.0], name="Merged", state=IdentityGroup.State.MERGED)
        result = self.search(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        self.assertEqual(result["matches"], [])

    def test_consent_is_required_before_search_runs(self):
        withdraw_face_consent(self.owner)
        with override_settings(BIOMETRIC_CONSENT_REQUIRED=True):
            with self.assertRaises(ValidationError):
                self.search()

    def test_an_undecodable_query_photo_is_a_clear_validation_error(self):
        with patch("identities.services.cv2.imdecode", return_value=None):
            with self.assertRaises(ValidationError):
                self.search()

    def test_the_query_embedding_never_reaches_the_database(self):
        self.make_group(self.owner, [1.0, 0.0, 0.0])
        before = set(IdentityFace.objects.values_list("pk", flat=True))
        self.search(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        after = set(IdentityFace.objects.values_list("pk", flat=True))
        self.assertEqual(before, after)


class VisualSearchApiTests(VisualSearchTestCase):
    def url(self):
        return reverse("identities:visual-search")

    def post(self, provider=None, detector=None):
        with (
            patch("identities.services.get_identity_provider", return_value=provider or make_provider()),
            patch("identities.services.get_face_detector", return_value=detector or ONE_FACE_DETECTOR),
        ):
            return self.client.post(
                self.url(), {"query_image": query_file()}, format="multipart",
            )

    def test_login_is_required(self):
        anonymous = APIClient()
        response = anonymous.post(self.url(), {"query_image": query_file()}, format="multipart")
        self.assertEqual(response.status_code, 401)

    def test_a_missing_file_is_a_clean_400(self):
        response = self.client.post(self.url(), {}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("query_image", response.data)

    def test_no_face_in_the_photo_gives_faces_detected_false(self):
        response = self.post(detector=NO_FACE_DETECTOR)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["faces_detected"])
        self.assertEqual(response.data["matches"], [])

    def test_a_match_includes_the_groups_photos_with_thumbnail_and_download_urls(self):
        group = self.make_group(self.owner, [1.0, 0.0, 0.0])
        photo = self.add_photo_to_group(group)
        response = self.post(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        self.assertEqual(response.status_code, 200)
        match = response.data["matches"][0]
        self.assertEqual(match["group_id"], group.pk)
        self.assertEqual(match["display_name"], "Person 001")
        self.assertEqual(match["photo_count"], 1)
        self.assertEqual(match["photos"][0]["id"], photo.pk)
        self.assertTrue(match["photos"][0]["thumbnail_url"].endswith(f"/api/images/{photo.pk}/thumbnail/"))
        self.assertTrue(match["photos"][0]["download_url"].endswith(f"/api/images/{photo.pk}/download/"))

    def test_removed_membership_photos_are_not_listed(self):
        group = self.make_group(self.owner, [1.0, 0.0, 0.0])
        self.add_photo_to_group(group)
        IdentityMembership.objects.update(state=IdentityMembership.State.REMOVED)
        response = self.post(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        self.assertEqual(response.data["matches"][0]["photo_count"], 0)

    def test_without_consent_the_search_is_rejected(self):
        withdraw_face_consent(self.owner)
        with override_settings(BIOMETRIC_CONSENT_REQUIRED=True):
            response = self.post()
        self.assertEqual(response.status_code, 400)

    def test_the_response_never_contains_an_embedding_vector(self):
        group = self.make_group(self.owner, [1.0, 0.0, 0.0])
        self.add_photo_to_group(group)
        response = self.post(provider=make_provider(vector=(1.0, 0.0, 0.0)))
        text = json.dumps(response.data)
        for forbidden in ("protected_embedding", "protected_centroid", "embedding", "centroid"):
            self.assertNotIn(forbidden, text)

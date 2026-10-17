"""A consent-gated endpoint that refuses answers 400 with the same sentence and a stable code."""
import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from identities.models import IdentityGroup
from uploads.models import ImageUpload, UploadBatch

from .testing import grant_face_consent, withdraw_face_consent

User = get_user_model()


@override_settings(BIOMETRIC_CONSENT_REQUIRED=True)
class ConsentRequiredCodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="consent-code", email="consent-code@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        withdraw_face_consent(self.user)

    def test_identity_analysis_without_consent(self):
        batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        image = ImageUpload.objects.create(owner=self.user, batch=batch, original_name="a.jpg", status="uploaded")
        response = self.client.post(reverse("identities:image-analysis", args=[image.pk]), {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {
            "detail": ["Biometric consent is required before identity analysis."], "code": "consent_required",
        })

    def test_face_search_without_consent(self):
        ok, encoded = cv2.imencode(".jpg", np.zeros((40, 40, 3), dtype=np.uint8))
        self.assertTrue(ok)
        upload = SimpleUploadedFile("query.jpg", encoded.tobytes(), content_type="image/jpeg")
        response = self.client.post(reverse("identities:visual-search"), {"query_image": upload}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {
            "detail": ["Biometric consent is required before face search."], "code": "consent_required",
        })

    def test_person_collage_without_consent(self):
        group = IdentityGroup.objects.create(
            owner=self.user, display_name="Someone", member_count=0, protected_centroid=b"c",
            embedding_dimension=3, provider="test", policy_version="v1",
        )
        response = self.client.post(
            reverse("collage-list"), {"source_type": "person", "group_id": group.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {
            "detail": "Biometric consent is required to make a collage of a person.", "code": "consent_required",
        })

    def test_other_validation_errors_carry_no_consent_code(self):
        grant_face_consent(self.user)
        response = self.client.post(reverse("identities:visual-search"), {}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("code", response.json())

    def test_event_collages_are_not_consent_gated(self):
        response = self.client.post(reverse("collage-list"), {"source_type": "event", "event_id": 999}, format="json")
        self.assertNotIn("consent_required", response.content.decode())

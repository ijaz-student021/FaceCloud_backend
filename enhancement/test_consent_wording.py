"""Face restoration needs the person's face-processing consent: when it was called for and not given, the result says so in plain words (the summary, `skipped_stages`, `hd_unavailable_reason`),
and only then. And the consent state is readable from one endpoint (`GET /api/privacy/consents/`)."""
from unittest.mock import patch

import cv2
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from privacy.models import ConsentPurpose
from privacy.services import consent_status, record_consent

from .adaptive.service import FACE_CONSENT_SENTENCE, AdaptiveImageEnhancementService, EngineConfig
from .restoration import describe_stages
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait

FACE = (150, 100, 180, 220)


def blurred_face():
    image = portrait(face_box=FACE)
    x, y, w, h = FACE
    image[y:y + h, x:x + w] = cv2.GaussianBlur(image[y:y + h, x:x + w], (0, 0), 6.0)
    return image


@override_settings(**ALL_ON, ADAPTIVE_ALWAYS_ENHANCE=False)
class ConsentSentenceTests(SimpleTestCase):
    def run_engine(self, image, allow):
        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(), config=EngineConfig(), runners={})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            return service.enhance(image, allow_biometric_processing=allow)[2]

    def test_a_degraded_face_without_consent_says_so_in_plain_words_everywhere_it_reaches_the_api(self):
        parameters = self.run_engine(blurred_face(), allow=False)
        adaptive = parameters["adaptive"]
        self.assertIn(FACE_CONSENT_SENTENCE, adaptive["summary"])
        described = describe_stages(parameters)
        face = [item for item in described["skipped_stages"] if item["stage"] == "face_restore"]
        self.assertEqual(len(face), 1)
        self.assertIn("face-processing consent", face[0]["reason"])
        self.assertIn("face-processing consent", described["hd_unavailable_reason"])
        self.assertNotIn("Biometric", adaptive["summary"] + face[0]["reason"] + described["hd_unavailable_reason"])      # the old technical wording is gone

    def test_a_photograph_whose_face_did_not_need_restoring_is_not_told_about_consent(self):
        parameters = self.run_engine(portrait(), allow=False)
        self.assertNotIn("consent", parameters["adaptive"]["summary"])
        self.assertFalse(any(item.get("needs_consent") for item in parameters["adaptive"]["operations_skipped"]))
        self.assertEqual(describe_stages(parameters)["skipped_stages"], [])

    def test_with_consent_the_sentence_is_not_added(self):
        parameters = self.run_engine(blurred_face(), allow=True)
        self.assertNotIn("consent", parameters["adaptive"]["summary"])
        self.assertFalse(any(item.get("needs_consent") for item in parameters["adaptive"]["operations_skipped"]))

    def test_a_kept_original_with_a_degraded_face_is_not_called_a_photograph_that_needed_nothing(self):
        summary = self.run_engine(blurred_face(), allow=False)["adaptive"]["summary"]
        self.assertNotIn("did not need enhancement", summary)


class ConsentEndpointTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="consenter", email="consenter@example.com", password="Password!123", is_email_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @override_settings(BIOMETRIC_CONSENT_REQUIRED=True)
    def test_one_endpoint_says_whether_faces_may_be_processed_now(self):
        body = self.client.get("/api/privacy/consents/").json()
        self.assertEqual((body["required"], body["face_processing_allowed"]), (True, False))
        purpose = body["purposes"][0]
        self.assertEqual((purpose["purpose"], purpose["granted"], purpose["active"]), ("face_identity", False, False))
        self.assertTrue(any("restoration" in line for line in purpose["covers"]))
        record_consent(self.user, ConsentPurpose.FACE_IDENTITY, True, body["current_policy_version"])
        self.assertTrue(self.client.get("/api/privacy/consents/").json()["face_processing_allowed"])
        record_consent(self.user, ConsentPurpose.FACE_IDENTITY, False, body["current_policy_version"])
        self.assertFalse(consent_status(self.user)["face_processing_allowed"])

    @override_settings(BIOMETRIC_CONSENT_REQUIRED=False)
    def test_when_consent_is_not_required_faces_may_be_processed(self):
        self.assertTrue(self.client.get("/api/privacy/consents/").json()["face_processing_allowed"])

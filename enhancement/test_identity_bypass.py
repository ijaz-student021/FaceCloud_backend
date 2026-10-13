"""AI Max skips the identity requirement for face restoration; that must be said.

Mocked: GFPGAN and the identity guard are stand-ins. These tests establish what
is *reported*, not what a real model does to a face.
"""
from unittest.mock import patch

import cv2
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from .models import ImageEnhancementResult
from .serializers import EnhancementRequestSerializer, ImageEnhancementResultSerializer
from .services import enhance_bgr, process_enhancement_result
from .test_pipeline_regressions import NO_MODELS, ResultFixture, textured
from .test_staged_pipeline import GFPGAN_ON, gentle_restoration


def guard_returning(status):
    return lambda source, candidate, reference=None: (
        candidate, {"status": status, "applied": False, "reference_checked": True},
    )


class IdentityVerificationSummaryTests(SimpleTestCase):
    def setUp(self):
        self.blurred = cv2.GaussianBlur(textured(720, 960, base=130), (0, 0), 6)  # eligible for restoration

    @override_settings(**GFPGAN_ON)
    @patch("enhancement.services.restore_faces", side_effect=gentle_restoration)
    def test_ai_max_restoring_faces_without_a_check_says_so_everywhere(self, _restore):
        _output, _level, parameters = enhance_bgr(self.blurred, "low", style="ai_max", allow_biometric_processing=False)

        summary = parameters["identity_verification"]
        self.assertEqual(summary["status"], "unavailable")
        self.assertFalse(summary["verified"])
        self.assertEqual(summary["requirement_bypass"], "ai_max_style")
        self.assertFalse(summary["required_for_face_restoration"])
        self.assertTrue(summary["face_restoration_applied"])
        self.assertTrue(summary["face_restoration_unverified"])
        self.assertIn("without a verified identity check", summary["notice"])
        self.assertIn("AI Max", summary["notice"])
        self.assertIn(summary["notice"], parameters["warnings"])
        self.assertEqual(parameters["identity_status"], "unavailable")
        self.assertEqual(parameters["face_restoration"]["identity_gate"], "bypassed_ai_max")
        self.assertFalse(parameters["face_restoration"]["identity_embedding_verified"])

    @override_settings(**GFPGAN_ON)
    @patch("enhancement.services.apply_identity_guard")
    @patch("enhancement.services.restore_faces", side_effect=gentle_restoration)
    def test_no_skipped_or_failed_check_is_ever_reported_as_verified(self, _restore, guard):
        for guard_status, expected in (
            ("skipped", "inconclusive"), ("inconclusive", "inconclusive"), ("unresolved", "failed"),
        ):
            guard.side_effect = guard_returning(guard_status)
            with patch("enhancement.services._identity_model_configured", return_value=True):
                parameters = enhance_bgr(self.blurred, "low", style="ai_max")[2]
            summary = parameters["identity_verification"]
            self.assertEqual(summary["status"], expected, guard_status)
            self.assertFalse(summary["verified"], guard_status)
            self.assertTrue(summary["face_restoration_unverified"], guard_status)
            self.assertFalse(parameters["face_restoration"]["identity_embedding_verified"], guard_status)

    @override_settings(**GFPGAN_ON)
    @patch("enhancement.services.apply_identity_guard")
    @patch("enhancement.services.restore_faces", side_effect=gentle_restoration)
    def test_ai_max_is_reported_verified_only_when_the_comparison_really_held(self, _restore, guard):
        guard.side_effect = guard_returning("preserved")
        parameters = enhance_bgr(self.blurred, "low", style="ai_max")[2]

        summary = parameters["identity_verification"]
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["requirement_bypass"], "ai_max_style")  # the bypass existed...
        self.assertFalse(summary["face_restoration_unverified"])          # ...but was not relied on
        self.assertEqual(summary["notice"], "")
        self.assertTrue(parameters["face_restoration"]["identity_embedding_verified"])

    @override_settings(**NO_MODELS)
    def test_ai_max_without_face_restoration_still_notes_that_nothing_was_verified(self):
        parameters = enhance_bgr(self.blurred, "low", style="ai_max")[2]
        summary = parameters["identity_verification"]
        self.assertFalse(summary["face_restoration_applied"])
        self.assertFalse(summary["face_restoration_unverified"])
        self.assertIn("identity was not verified", summary["notice"])
        self.assertNotIn(summary["notice"], parameters["warnings"])  # informative, not a warning

    @override_settings(**GFPGAN_ON)
    @patch("enhancement.services.apply_identity_guard")
    @patch("enhancement.services.build_identity_reference", return_value=(object(), ""))
    @patch("enhancement.services.restore_faces", side_effect=gentle_restoration)
    def test_natural_mode_reports_the_requirement_as_enforced(self, _restore, _reference, guard):
        guard.side_effect = guard_returning("preserved")
        parameters = enhance_bgr(self.blurred, "low")[2]

        summary = parameters["identity_verification"]
        self.assertTrue(summary["required_for_face_restoration"])
        self.assertIsNone(summary["requirement_bypass"])
        self.assertEqual(parameters["face_restoration"]["identity_gate"], "passed")
        self.assertTrue(summary["verified"])

    @override_settings(**{**GFPGAN_ON, "GFPGAN_REQUIRE_IDENTITY_CHECK": False})
    @patch("enhancement.services.restore_faces", side_effect=gentle_restoration)
    def test_an_operator_switching_the_requirement_off_is_named_as_such(self, _restore):
        parameters = enhance_bgr(self.blurred, "low", allow_biometric_processing=False)[2]

        summary = parameters["identity_verification"]
        self.assertEqual(summary["requirement_bypass"], "operator_setting")
        self.assertTrue(summary["face_restoration_unverified"])
        self.assertIn("this deployment does not require", summary["notice"])
        self.assertEqual(parameters["face_restoration"]["identity_gate"], "not_enforced")


class IdentityBypassApiTests(ResultFixture):
    def serialized(self, result):
        return ImageEnhancementResultSerializer(result).data

    @override_settings(**GFPGAN_ON)
    @patch("enhancement.services.restore_faces", side_effect=gentle_restoration)
    def test_the_result_endpoint_states_the_bypass(self, _restore):
        result = self.make_result(size=(720, 960))
        ImageEnhancementResult.objects.filter(pk=result.pk).update(requested_style="ai_max")
        blurred = cv2.GaussianBlur(textured(720, 960, base=130), (0, 0), 6)
        with patch("enhancement.services._read_enhancement_source", return_value=(blurred, "full_resolution_original")), \
                patch("privacy.services.biometric_processing_allowed", return_value=False):
            result = process_enhancement_result(result)

        client = APIClient()
        client.force_authenticate(self.user)
        body = client.get(f"/api/enhancements/results/{result.pk}/").json()

        self.assertEqual(body["requested_style"], "ai_max")
        self.assertEqual(body["identity_status"], "unavailable")
        self.assertIs(body["identity_check_bypassed"], True)
        self.assertIn("without a verified identity check", body["identity_notice"])

    @override_settings(**NO_MODELS)
    def test_an_ordinary_result_reports_no_bypass(self):
        data = self.serialized(process_enhancement_result(self.make_result()))
        self.assertEqual(data["identity_status"], "unavailable")
        self.assertIs(data["identity_check_bypassed"], False)
        self.assertEqual(data["identity_notice"], "")

    def test_results_from_before_these_fields_existed_still_serialize(self):
        result = self.make_result(status="completed")
        for stored in ({}, {"identity_status": "verified"}, {"identity_verification": "garbage"}):
            ImageEnhancementResult.objects.filter(pk=result.pk).update(applied_parameters=stored)
            data = self.serialized(ImageEnhancementResult.objects.get(pk=result.pk))
            self.assertIs(data["identity_check_bypassed"], False)
            self.assertEqual(data["identity_notice"], "")
            self.assertEqual(data["identity_status"], stored.get("identity_status") if isinstance(stored, dict) else None)

    def test_the_request_schema_warns_about_ai_max_before_it_is_chosen(self):
        help_text = EnhancementRequestSerializer().fields["style"].help_text
        self.assertIn("ai_max", help_text)
        self.assertIn("without a verified identity check", help_text)

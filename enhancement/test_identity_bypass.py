"""What a result says about identity.

Face restoration, the only thing that could put a face in a result that the
identity check had not confirmed, was retired on 2026-10-15. Results are now
described by the identity guard alone: ``verified`` is true only when the saved
image's faces were compared with the source's and held. The API fields that
reported a bypass (``identity_check_bypassed``, ``identity_notice``) remain for
compatibility: they are false/empty for every new result and still report what
an earlier result stored.
"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from .models import ImageEnhancementResult
from .serializers import EnhancementRequestSerializer, ImageEnhancementResultSerializer
from .services import enhance_bgr, process_enhancement_result
from .test_blur_measure import blurred_photo
from .test_pipeline_regressions import NO_MODELS, ResultFixture, textured

IDENTITY_ON = {**NO_MODELS, "IDENTITY_GUARD_ENABLED": True}


def guard_returning(status):
    return lambda source, candidate: (candidate, {"status": status, "applied": False, "reference_checked": True})


class IdentityVerificationSummaryTests(SimpleTestCase):
    @override_settings(**IDENTITY_ON)
    @patch("enhancement.services.apply_identity_guard")
    def test_no_skipped_or_failed_check_is_ever_reported_as_verified(self, guard):
        for guard_status, expected in (
            ("skipped", "inconclusive"), ("inconclusive", "inconclusive"), ("unresolved", "failed"),
        ):
            guard.side_effect = guard_returning(guard_status)
            with patch("enhancement.services._identity_model_configured", return_value=True):
                parameters = enhance_bgr(textured(), "low", style="ai_max")[2]
            summary = parameters["identity_verification"]
            self.assertEqual(summary["status"], expected, guard_status)
            self.assertFalse(summary["verified"], guard_status)

    @override_settings(**IDENTITY_ON)
    @patch("enhancement.services.apply_identity_guard")
    def test_a_comparison_that_held_is_reported_verified(self, guard):
        guard.side_effect = guard_returning("preserved")
        summary = enhance_bgr(textured(), "low", style="ai_max")[2]["identity_verification"]
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["status"], "verified")
        self.assertEqual(summary["notice"], "")

    @override_settings(**NO_MODELS)
    def test_the_summary_has_only_what_is_still_true(self):
        summary = enhance_bgr(blurred_photo(), "low", style="ai_max")[2]["identity_verification"]
        self.assertEqual(set(summary), {"status", "verified", "reason", "notice"})
        self.assertEqual((summary["status"], summary["verified"]), ("unavailable", False))


class IdentityFieldsOfStoredResultsTests(ResultFixture):
    def serialized(self, result):
        return ImageEnhancementResultSerializer(result).data

    @override_settings(**NO_MODELS)
    def test_an_ordinary_result_reports_no_bypass(self):
        data = self.serialized(process_enhancement_result(self.make_result()))
        self.assertEqual(data["identity_status"], "unavailable")
        self.assertIs(data["identity_check_bypassed"], False)
        self.assertEqual(data["identity_notice"], "")

    @override_settings(**NO_MODELS)
    def test_an_ai_max_result_reports_no_bypass_either(self):
        result = self.make_result(size=(720, 960))
        ImageEnhancementResult.objects.filter(pk=result.pk).update(requested_style="ai_max")
        result = process_enhancement_result(result)

        client = APIClient()
        client.force_authenticate(self.user)
        body = client.get(f"/api/enhancements/results/{result.pk}/").json()

        self.assertEqual(body["requested_style"], "ai_max")
        self.assertIs(body["identity_check_bypassed"], False)
        self.assertEqual(body["identity_notice"], "")
        self.assertIn("retired", " ".join(result.applied_parameters["warnings"]))

    def test_a_result_made_while_face_restoration_existed_still_reports_what_it_stored(self):
        result = self.make_result(status="completed")
        stored = {
            "algorithm": "zero_dce_hybrid_v1+private_gfpgan_v1_4",
            "identity_status": "unavailable",
            "identity_verification": {
                "status": "unavailable", "verified": False, "requirement_bypass": "ai_max_style",
                "face_restoration_applied": True, "face_restoration_unverified": True,
                "notice": "Faces in this image were generatively restored without a verified identity check.",
            },
            "face_restoration": {"applied": True, "provider": "private_gfpgan_v1_4"},
        }
        ImageEnhancementResult.objects.filter(pk=result.pk).update(applied_parameters=stored)

        data = self.serialized(ImageEnhancementResult.objects.get(pk=result.pk))

        self.assertIs(data["identity_check_bypassed"], True)
        self.assertIn("without a verified identity check", data["identity_notice"])
        self.assertEqual(data["identity_status"], "unavailable")
        self.assertEqual(data["applied_parameters"]["algorithm"], stored["algorithm"])  # history is not rewritten

    def test_results_from_before_these_fields_existed_still_serialize(self):
        result = self.make_result(status="completed")
        for stored in ({}, {"identity_status": "verified"}, {"identity_verification": "garbage"}):
            ImageEnhancementResult.objects.filter(pk=result.pk).update(applied_parameters=stored)
            data = self.serialized(ImageEnhancementResult.objects.get(pk=result.pk))
            self.assertIs(data["identity_check_bypassed"], False)
            self.assertEqual(data["identity_notice"], "")
            self.assertEqual(data["identity_status"], stored.get("identity_status") if isinstance(stored, dict) else None)

    def test_the_request_schema_no_longer_promises_face_restoration(self):
        help_text = EnhancementRequestSerializer().fields["style"].help_text
        self.assertIn("ai_max", help_text)
        self.assertIn("never restores faces", help_text)
        self.assertNotIn("may restore faces", help_text)

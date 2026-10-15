"""The model-comparison endpoint reports identity the way the result endpoint does.

The identity guard is a stand-in, so these tests cover what each endpoint *reports*
about a profile, not what a real model does to a face. (Face restoration, which could
put an unchecked face in a result, was retired on 2026-10-15; the bypass fields stay
for compatibility and are false for every new output.)
"""
import csv
import io
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .comparison import ORIGINAL, REPORT_COLUMNS, build_report_csv, process_model_comparison, recommend
from .models import ImageEnhancementResult, ModelComparison, ModelComparisonOutput
from .serializers import (
    ImageEnhancementResultSerializer,
    ModelComparisonOutputSerializer,
    identity_fields,
)
from .services import process_enhancement_result
from .test_comparison import ComparisonFixtureMixin, candidate, metrics
from .test_pipeline_regressions import NO_MODELS

FIELDS = ("identity_status", "identity_check_bypassed", "identity_notice")
SOURCE = {"overall_score": 50.0, "metrics": metrics(), "faces_detected": 1}


def guard_returning(status):
    return lambda source, candidate: (
        candidate, {"status": status, "applied": False, "reference_checked": True},
    )


class IdentityFieldsTests(SimpleTestCase):
    def test_one_function_defines_the_three_fields(self):
        summary = {"status": "unavailable", "face_restoration_unverified": True, "notice": "Faces were restored."}
        self.assertEqual(identity_fields(summary), ("unavailable", True, "Faces were restored."))

    def test_missing_or_malformed_summaries_never_claim_anything(self):
        for stored in (None, {}, "garbage", ["verified"], {"status": ""}):
            self.assertEqual(identity_fields(stored), (None, False, ""), stored)

    def test_a_legacy_status_is_used_only_when_there_is_no_summary_status(self):
        self.assertEqual(identity_fields({}, "inconclusive")[0], "inconclusive")
        self.assertEqual(identity_fields({"status": "failed"}, "verified")[0], "failed")


class RecommendationNoteTests(SimpleTestCase):
    def unverified(self, profile, score):
        item = candidate(profile, score)
        item["identity_guard"] = {"status": "skipped", "verification": {"face_restoration_unverified": True}}
        return item

    def test_a_recommended_profile_whose_stored_output_has_unverified_restored_faces_says_so(self):
        """Only a comparison made before face restoration was retired can carry this."""
        profile, reason, details = recommend(SOURCE, [candidate("natural", 60), self.unverified("ai_max", 75)])

        self.assertEqual(profile, "ai_max")
        self.assertIn("without a verified identity check", reason)
        self.assertIn("without a verified identity check", details["ai_max"]["notes"][0])

    def test_the_note_is_information_and_does_not_change_the_score(self):
        _profile, _reason, details = recommend(SOURCE, [self.unverified("ai_max", 75)])
        self.assertEqual(details["ai_max"]["penalty"], 0)
        self.assertEqual(details["ai_max"]["adjusted_score"], 75.0)

    def test_a_verified_profile_carries_no_such_note(self):
        _profile, reason, details = recommend(SOURCE, [candidate("ai_max", 75, guard="preserved")])
        self.assertEqual(details["ai_max"]["notes"], [])
        self.assertNotIn("identity check", reason)


@override_settings(**{**NO_MODELS, "IDENTITY_GUARD_ENABLED": True})
class ComparisonIdentityTests(ComparisonFixtureMixin, TestCase):
    def setUp(self):
        self.user = self.make_user("compare-identity")
        self.image = self.make_image(self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def run_comparison(self, profiles=("natural", "ai_max")):
        comparison = ModelComparison.objects.create(
            owner=self.user, image=self.image, status="queued", profiles=list(profiles),
        )
        return process_model_comparison(comparison)

    def api_outputs(self, comparison):
        response = self.client.get(reverse("enhancement:comparison-detail", kwargs={"pk": comparison.pk}))
        self.assertEqual(response.status_code, 200)
        return {output["profile"]: output for output in response.json()["outputs"]}

    @patch("privacy.services.biometric_processing_allowed", return_value=False)
    def test_without_consent_every_profile_says_identity_was_not_checked(self, _consent):
        outputs = self.api_outputs(self.run_comparison())

        ai_max, natural, original = outputs["ai_max"], outputs["natural"], outputs[ORIGINAL]
        for output in (ai_max, natural):
            self.assertEqual(output["identity_status"], "unavailable")
            self.assertIs(output["identity_check_bypassed"], False)
            self.assertEqual(output["identity_notice"], "")
        # The untouched source was not enhanced, so there is nothing to report.
        self.assertEqual([original[name] for name in FIELDS], [None, False, ""])

    @patch("privacy.services.biometric_processing_allowed", return_value=False)
    def test_a_profile_reads_the_same_here_as_on_the_result_it_would_produce(self, _consent):
        """The regression: the two endpoints must not disagree about one outcome."""
        compared = self.api_outputs(self.run_comparison())

        for style, profile in (("ai_max", "ai_max"), ("natural", "natural")):
            result = ImageEnhancementResult.objects.create(
                owner=self.user, image=self.image, preprocessing_result=self.image.preprocessing_result,
                status="queued", requested_style=style,
            )
            result = process_enhancement_result(result)
            body = self.client.get(f"/api/enhancements/results/{result.pk}/").json()
            self.assertEqual(
                [compared[profile][name] for name in FIELDS], [body[name] for name in FIELDS], profile,
            )
            ImageEnhancementResult.objects.filter(pk=result.pk).delete()

    @patch("enhancement.services.apply_identity_guard")
    def test_verified_is_reported_only_when_the_comparison_held(self, guard):
        for guard_status, expected in (("preserved", "verified"), ("inconclusive", "inconclusive"), ("unresolved", "failed")):
            guard.side_effect = guard_returning(guard_status)
            with patch("enhancement.services._identity_model_configured", return_value=True):
                outputs = self.api_outputs(self.run_comparison(profiles=("ai_max",)))
            self.assertEqual(outputs["ai_max"]["identity_status"], expected, guard_status)
            self.assertIs(outputs["ai_max"]["identity_check_bypassed"], False, guard_status)
            self.assertEqual(outputs["ai_max"]["identity_notice"], "", guard_status)

    @patch("privacy.services.biometric_processing_allowed", return_value=False)
    def test_the_csv_report_carries_the_same_three_values(self, _consent):
        comparison = self.run_comparison()
        rows = {row["profile"]: row for row in csv.DictReader(io.StringIO(build_report_csv(comparison)))}

        self.assertEqual(rows["ai_max"]["identity_status"], "unavailable")
        self.assertEqual(rows["ai_max"]["identity_check_bypassed"], "")
        self.assertEqual(rows["natural"]["identity_check_bypassed"], "")
        self.assertEqual(rows[ORIGINAL]["identity_status"], "")
        # Appended at the end: readers of the earlier columns are unaffected.
        self.assertEqual(REPORT_COLUMNS[-3:], FIELDS)
        self.assertEqual(REPORT_COLUMNS[-5:-3], ("processing_ms", "algorithm"))

    def test_a_stored_output_from_before_the_retirement_still_reports_its_bypass_in_the_csv(self):
        comparison = ModelComparison.objects.create(
            owner=self.user, image=self.image, status="completed", profiles=["ai_max"],
        )
        ModelComparisonOutput.objects.create(
            comparison=comparison, profile="ai_max", outcome="completed", algorithm="zero_dce_hybrid_v1+private_gfpgan_v1_4",
            overall_score=60.0,
            identity_guard={"status": "skipped", "verification": {
                "status": "unavailable", "face_restoration_unverified": True, "notice": "Faces were restored."}},
        )
        rows = {row["profile"]: row for row in csv.DictReader(io.StringIO(build_report_csv(comparison)))}
        self.assertEqual(rows["ai_max"]["identity_check_bypassed"], "yes")
        self.assertEqual(rows["ai_max"]["identity_notice"], "Faces were restored.")

    def test_outputs_stored_before_these_fields_existed_and_failed_profiles_still_serialize(self):
        comparison = ModelComparison.objects.create(
            owner=self.user, image=self.image, status="completed", profiles=["natural", "ai_max"],
        )
        legacy = ModelComparisonOutput.objects.create(
            comparison=comparison, profile="natural", outcome="completed",
            identity_guard={"status": "preserved", "similarity": 0.91, "strength": 1.0, "faces_compared": 1},
        )
        failed = ModelComparisonOutput.objects.create(
            comparison=comparison, profile="ai_max", outcome="failed",
            failure_reason="This profile could not process the image safely.",
        )
        for output in (legacy, failed):
            data = ModelComparisonOutputSerializer(output).data
            # An old record has no summary: it must not be upgraded to "verified"
            # on the strength of a guard status this field never described.
            self.assertEqual([data[name] for name in FIELDS], [None, False, ""], output.profile)
        self.assertEqual(ModelComparisonOutputSerializer(legacy).data["identity_guard"]["status"], "preserved")

    def test_the_result_and_comparison_serializers_share_the_field_descriptions(self):
        result_fields = ImageEnhancementResultSerializer().fields
        output_fields = ModelComparisonOutputSerializer().fields
        for name in FIELDS:
            self.assertEqual(result_fields[name].help_text, output_fields[name].help_text, name)

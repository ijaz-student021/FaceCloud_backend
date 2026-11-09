"""A face restoration is held to its own identity limit (SFACE_RESTORE_GUARD_MIN_SIMILARITY), and the router's fidelity attempts come from ADAPTIVE_FACE_FIDELITIES.

The restored face is compared with the face in the UPLOAD; when the upload is blurred its own embedding is poor, so a faithful restoration scores well below the guard's 0.80 against it. Measured on the
owner's photographs and on LFW in docs/evaluation/2026-11-04-face-fidelity.md. Stand-ins replace the identity model here (CI has none).
"""
from unittest.mock import patch

import cv2
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from identities.providers import ArcFaceOnnxProvider, SFaceOnnxProvider, thresholds_for

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig, IdentityChecker, identity_report
from .adaptive.validator import FinalImageQualityValidator

BOX = (150, 100, 180, 220)


class StubIdentity:
    """Says what the real checker says for a fixed similarity: its own two limits, a restoration held to the lower one."""

    available, threshold, restore_threshold, reason = True, 0.80, 0.65, ""

    def __init__(self, similarity):
        self.similarity = similarity

    def check(self, candidate, restoring=False):
        limit = self.restore_threshold if restoring else self.threshold
        return {"status": "verified" if self.similarity >= limit else "failed", "similarity": self.similarity, "threshold": limit}


class LimitSettingTests(SimpleTestCase):
    def test_sface_has_a_restoration_limit_of_its_own_below_its_guard(self):
        limits = thresholds_for(SFaceOnnxProvider())
        self.assertEqual((limits["guard"], limits["restore_guard"]), (0.80, 0.65))

    @override_settings(SFACE_RESTORE_GUARD_MIN_SIMILARITY=0.80)
    def test_setting_it_to_the_guard_restores_the_earlier_behaviour(self):
        self.assertEqual(thresholds_for(SFaceOnnxProvider())["restore_guard"], 0.80)

    @override_settings(SFACE_RESTORE_GUARD_MIN_SIMILARITY=0.95)
    def test_a_restoration_is_never_held_to_more_than_the_guard(self):
        self.assertEqual(thresholds_for(SFaceOnnxProvider())["restore_guard"], 0.80)

    def test_another_model_keeps_its_own_guard(self):
        limits = thresholds_for(ArcFaceOnnxProvider())
        self.assertEqual(limits["restore_guard"], limits["guard"])


class CheckerTests(SimpleTestCase):
    def checker(self, similarity):
        checker = IdentityChecker.__new__(IdentityChecker)
        checker.reference, checker.reason, checker.threshold, checker.restore_threshold = object(), "", 0.80, 0.65
        checker.faces = lambda candidate: [{"box": (0, 0, 1, 1), "similarity": similarity}]
        return checker

    def test_a_restoration_is_verified_at_a_similarity_the_guard_would_refuse(self):
        checker = self.checker(0.70)
        self.assertEqual(checker.check(None)["status"], "failed")
        restored = checker.check(None, restoring=True)
        self.assertEqual((restored["status"], restored["threshold"]), ("verified", 0.65))

    def test_below_the_restoration_limit_it_still_fails(self):
        self.assertEqual(self.checker(0.60).check(None, restoring=True)["status"], "failed")

    def test_the_result_reports_the_limit_that_was_applied(self):
        report = base.analyzer_with(BOX).analyze(base.portrait(face_box=BOX))
        block = identity_report(report, StubIdentity(0.70), [ops.FACE], StubIdentity(0.70).check(None, restoring=True), True)
        self.assertEqual((block["state"], block["threshold"]), ("verified", 0.65))


class ValidatorGateTests(SimpleTestCase):
    def gate(self, operation, similarity, identity=True):
        image = base.portrait(face_box=BOX)
        analyzer = base.analyzer_with(BOX)
        report = analyzer.analyze(image)
        validator = FinalImageQualityValidator(analyzer, report, StubIdentity(similarity) if identity else None)
        verdict = {"gates": {}, "reasons": []}
        validator._identity_gate(verdict, image.copy(), analyzer.analyze(image), operation)
        return verdict

    def test_a_restored_face_at_0_70_passes_the_identity_gate(self):
        verdict = self.gate(ops.FACE, 0.70)
        self.assertTrue(verdict["gates"]["identity"]["passed"], verdict)
        self.assertEqual(verdict["reasons"], [])

    def test_any_other_operation_at_0_70_is_still_refused(self):
        for operation in (ops.LOW_LIGHT, ops.DEBLUR, ops.DETAIL, ops.UPSCALE):
            verdict = self.gate(operation, 0.70)
            self.assertIn("identity_changed", verdict["reasons"], operation)

    def test_a_restored_face_below_the_limit_is_refused(self):
        self.assertIn("identity_changed", self.gate(ops.FACE, 0.60)["reasons"])

    def test_an_unverifiable_restoration_is_still_never_accepted(self):
        verdict = self.gate(ops.FACE, 0.99, identity=False)
        self.assertIn("identity_unverifiable", verdict["reasons"])


class FidelityLadderTests(SimpleTestCase):
    def face_step(self):
        image = base.portrait(face_box=BOX)
        x, y, w, h = BOX
        image[y:y + h, x:x + w] = cv2.GaussianBlur(image[y:y + h, x:x + w], (0, 0), 6.0)
        report = base.analyzer_with(BOX).analyze(image)
        decision = AdaptiveEnhancementRouter(EngineConfig()).next_step(report, {ops.LOW_LIGHT, ops.DENOISE, ops.DEBLUR})
        return decision.step

    @override_settings(**base.ALL_ON)
    def test_the_attempts_are_the_setting_in_order(self):
        step = self.face_step()
        self.assertEqual(step.operation, ops.FACE)
        self.assertEqual([variant["fidelity"] for variant in step.variants], list(settings.ADAPTIVE_FACE_FIDELITIES))
        self.assertEqual(settings.ADAPTIVE_FACE_FIDELITIES, (1.0,))          # the measured default: one attempt at the most faithful weight

    @override_settings(**{**base.ALL_ON, "ADAPTIVE_FACE_FIDELITIES": (1.0, 0.85)})
    def test_another_ladder_is_used_as_given(self):
        step = self.face_step()
        self.assertEqual([(variant["name"], variant["fidelity"]) for variant in step.variants], [("fidelity 1", 1.0), ("fidelity 0.85", 0.85)])


class EndToEndTests(SimpleTestCase):
    """Through the whole engine: the candidate AND the saved photograph's identity state use the restoration limit."""

    def run_with(self, similarity):
        sharp = base.portrait(face_box=BOX)
        blurred = sharp.copy()
        x, y, w, h = BOX
        blurred[y:y + h, x:x + w] = cv2.GaussianBlur(blurred[y:y + h, x:x + w], (0, 0), 6.0)

        class Identity(StubIdentity):
            def __init__(self, original, allowed):
                super().__init__(similarity)

            def faces(self, candidate):
                return [{"box": BOX, "similarity": self.similarity}]

        restored = sharp.copy()
        restored[y:y + h, x:x + w] = cv2.GaussianBlur(restored[y:y + h, x:x + w], (0, 0), 1.2)       # clearer than the blurred face, and no noisier than the photograph
        service = AdaptiveImageEnhancementService(analyzer=base.analyzer_with(BOX), config=EngineConfig(), runners={ops.FACE: lambda image, engine, variant: (restored.copy(), [{"model": engine}])})
        with patch("enhancement.adaptive.service.IdentityChecker", Identity), override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False}):
            return service.enhance(blurred)[2]

    def test_a_faithful_restoration_below_the_guard_is_applied_and_reported_verified_at_its_own_limit(self):
        parameters = self.run_with(0.70)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.FACE], parameters["adaptive"]["stages"])
        block = parameters["identity_verification"]
        self.assertEqual((block["state"], block["status"], block["threshold"]), ("verified", "verified", 0.65))

    def test_below_the_restoration_limit_it_is_refused(self):
        parameters = self.run_with(0.60)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [])
        stage = next(stage for stage in parameters["adaptive"]["stages"] if stage["operation"] == ops.FACE)
        self.assertIn("identity_changed", stage["rejection_reasons"])

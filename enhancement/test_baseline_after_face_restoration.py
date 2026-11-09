"""After an accepted face restoration the steps that follow are judged against the RESTORED face (ADAPTIVE_BASELINE_AFTER_FACE_RESTORATION, service.RestoredBaseline).

The case: a restored face scored 0.807 against the upload (the restoration limit is 0.65), and the enlargement of that photograph scored 0.7993 against the upload and was refused for the ordinary 0.80, so
the photograph lost its enlargement and its detail layer by 0.0007 (docs/evaluation/2026-11-05-baseline-after-face-restoration.md). A later step is now held to the RESTORATION limit against the UPLOAD (the
limit the saved photograph's identity is judged at) and to the ORDINARY limit against the RESTORED face. The identity model here is a stand-in (CI has none).
"""
from types import SimpleNamespace
from unittest.mock import patch

import cv2
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig, RestoredBaseline
from .adaptive.validator import FinalImageQualityValidator
from .test_adaptive_enlargement_geometry import BiasedDetector, clear_enlargement
from .test_adaptive_landmark_floor import face

BOX = (150, 100, 180, 220)


class Judge:
    """What IdentityChecker says for a fixed similarity: its own two limits, a restoration held to the lower one."""

    available, threshold, restore_threshold, reason, reference = True, 0.80, 0.65, "", object()

    def __init__(self, similarity):
        self.similarity = similarity
        self.calls = []

    def check(self, candidate, restoring=False):
        self.calls.append(restoring)
        if self.similarity is None:
            return {"status": "inconclusive", "similarity": None, "threshold": self.restore_threshold if restoring else self.threshold}
        limit = self.restore_threshold if restoring else self.threshold
        return {"status": "verified" if self.similarity >= limit else "failed", "similarity": self.similarity, "threshold": limit}


class RestoredBaselineTests(SimpleTestCase):
    def check(self, upload, restored, restoring=False):
        return RestoredBaseline(Judge(upload), None if restored is False else Judge(restored)).check(object(), restoring=restoring)

    def test_the_case_that_was_refused_by_0_0007_passes(self):
        """0.7993 against the upload (limit 0.65 now) and 0.99 against the restored face (limit 0.80)."""
        result = self.check(0.7993, 0.99)
        self.assertEqual(result["status"], "verified")

    def test_the_upload_is_held_to_the_restoration_limit_never_below(self):
        result = self.check(0.60, 0.99)
        self.assertEqual((result["status"], result["baseline"], result["threshold"]), ("failed", "upload", 0.65))

    def test_a_step_that_changes_the_restored_face_is_refused_whatever_the_upload_says(self):
        result = self.check(0.90, 0.70)
        self.assertEqual((result["status"], result["baseline"], result["threshold"]), ("failed", "restored face", 0.80))

    def test_the_comparison_with_the_least_room_is_the_one_reported(self):
        result = self.check(0.70, 0.99)             # 0.05 over the restoration limit, 0.19 over the ordinary one
        self.assertEqual((result["status"], result["baseline"]), ("verified", "upload"))
        result = self.check(0.99, 0.82)
        self.assertEqual((result["status"], result["baseline"]), ("verified", "restored face"))

    def test_a_failure_decides_even_when_the_other_comparison_has_less_room(self):
        result = self.check(0.66, 0.70)             # the upload has 0.01 of room, the restored face fails by 0.10
        self.assertEqual((result["status"], result["baseline"]), ("failed", "restored face"))

    def test_without_an_embedding_of_the_restored_face_only_the_upload_limit_applies(self):
        self.assertEqual(self.check(0.70, False)["status"], "verified")
        self.assertEqual(self.check(0.60, False)["status"], "failed")

    def test_a_comparison_that_could_not_be_made_does_not_decide(self):
        self.assertEqual(self.check(0.70, None)["status"], "verified")           # the restored face was not found again: the upload comparison stands
        self.assertEqual(self.check(None, 0.99)["status"], "verified")
        result = self.check(None, None)
        self.assertEqual(result["status"], "inconclusive")

    def test_the_upload_is_always_asked_at_the_restoration_limit_and_the_restored_face_at_the_ordinary_one(self):
        upload, restored = Judge(0.9), Judge(0.9)
        RestoredBaseline(upload, restored).check(object())
        self.assertEqual((upload.calls, restored.calls), ([True], [False]))

    def test_it_carries_what_the_report_reads_from_the_checker(self):
        baseline = RestoredBaseline(Judge(0.9), Judge(0.9))
        self.assertTrue(baseline.available)
        self.assertEqual((baseline.threshold, baseline.restore_threshold), (0.80, 0.65))


class ValidatorGateTests(SimpleTestCase):
    def gate(self, identity, operation="super_resolution"):
        original = SimpleNamespace(faces=[face((230, 100, 114, 179))], width=640)
        candidate = SimpleNamespace(faces=[face((230, 100, 114, 179), 2)], width=1280)
        verdict = {"reasons": [], "gates": {}}
        FinalImageQualityValidator(None, original, identity=identity)._identity_gate(verdict, None, candidate, operation)
        return verdict["gates"]["identity"], verdict["reasons"]

    def test_the_old_judge_refuses_it_and_the_new_one_does_not(self):
        status, reasons = self.gate(Judge(0.7993))
        self.assertFalse(status["passed"])
        self.assertIn("identity_changed", reasons)
        status, reasons = self.gate(RestoredBaseline(Judge(0.7993), Judge(0.99)))
        self.assertTrue(status["passed"], status)
        self.assertEqual(reasons, [])
        self.assertIn(status["baseline"], ("upload", "restored face"))

    def test_the_verdict_records_which_comparison_decided_and_its_limit(self):
        status, _ = self.gate(RestoredBaseline(Judge(0.9), Judge(0.7)))
        self.assertEqual((status["baseline"], status["threshold"], status["similarity"]), ("restored face", 0.80, 0.7))
        self.assertFalse(status["passed"])

    def test_a_plain_judge_records_no_baseline(self):
        self.assertNotIn("baseline", self.gate(Judge(0.95))[0])


ENGINE_SETTINGS = {**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False}


class EngineTests(SimpleTestCase):
    """Through the whole engine: a blurred face is restored (accepted at its own limit), then the photograph is enlarged."""

    def run_engine(self, upload_similarity=0.72, restored_similarity=0.97, restored_available=True, **settings_):
        sharp = base.portrait(face_box=BOX)
        blurred = sharp.copy()
        x, y, w, h = BOX
        blurred[y:y + h, x:x + w] = cv2.GaussianBlur(blurred[y:y + h, x:x + w], (0, 0), 6.0)
        restored = sharp.copy()
        restored[y:y + h, x:x + w] = cv2.GaussianBlur(restored[y:y + h, x:x + w], (0, 0), 1.2)
        built = []

        class Identity(Judge):
            def __init__(self, source, allowed):
                is_upload = not built
                super().__init__(None)
                built.append(source)
                self.available = is_upload or restored_available
                values = upload_similarity if is_upload else restored_similarity
                self.values = list(values) if isinstance(values, (list, tuple)) else [values]

            def check(self, candidate, restoring=False):
                assert self.available, "a checker with no reference cannot compare (the real one has nothing to compare with)"
                self.similarity = self.values.pop(0) if len(self.values) > 1 else self.values[0]        # a list is answered call by call; its last value stays
                return super().check(candidate, restoring)

        service = AdaptiveImageEnhancementService(
            analyzer=ImageQualityAnalyzer(detector=BiasedDetector(BOX, 0.0, blurred.shape[1])), config=EngineConfig(),
            runners={ops.FACE: lambda image, engine, variant: (restored.copy(), [{"model": engine}]), ops.UPSCALE: clear_enlargement})
        with patch("enhancement.adaptive.service.IdentityChecker", Identity), override_settings(**{**ENGINE_SETTINGS, **settings_}):
            _out, _level, parameters = service.enhance(blurred, style="ai_max")
        return parameters, built

    def stages(self, parameters):
        return {stage["operation"]: (stage["status"], stage.get("rejection_reasons")) for stage in parameters["adaptive"]["stages"]}

    def test_the_premise_without_the_baseline_the_enlargement_is_refused_for_identity(self):
        parameters, _ = self.run_engine(ADAPTIVE_BASELINE_AFTER_FACE_RESTORATION=False)
        stages = self.stages(parameters)
        self.assertEqual(stages[ops.FACE][0], "accepted")
        self.assertEqual(stages[ops.UPSCALE], ("rejected", ["identity_changed"]))
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.FACE])

    def test_with_the_baseline_the_enlargement_follows_the_restoration(self):
        parameters, built = self.run_engine()
        stages = self.stages(parameters)
        self.assertEqual((stages[ops.FACE][0], stages[ops.UPSCALE][0]), ("accepted", "accepted"), stages)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.FACE, ops.UPSCALE])
        self.assertEqual(len(built), 2, "the upload's faces, then the restored faces, each embedded once")

    def test_when_the_restored_face_cannot_be_embedded_the_upload_limit_alone_applies(self):
        parameters, built = self.run_engine(restored_available=False)
        stages = self.stages(parameters)
        self.assertEqual((stages[ops.FACE][0], stages[ops.UPSCALE][0]), ("accepted", "accepted"), stages)          # no crash, and the enlargement is judged at the restoration limit against the upload
        self.assertEqual(len(built), 2)
        parameters, _ = self.run_engine(upload_similarity=[0.70, 0.60], restored_available=False)
        self.assertEqual(self.stages(parameters)[ops.UPSCALE], ("rejected", ["identity_changed"]))

    def test_the_saved_photographs_identity_is_still_judged_against_the_upload_at_the_restoration_limit(self):
        parameters, _ = self.run_engine()
        block = parameters["identity_verification"]
        self.assertEqual((block["state"], block["status"], block["threshold"]), ("verified", "verified", 0.65))

    def test_a_step_that_changes_the_restored_face_is_still_refused(self):
        parameters, _ = self.run_engine(restored_similarity=0.70)               # the enlargement would be 0.70 from the restored face: over the 0.65 restoration limit, under the ordinary 0.80
        stages = self.stages(parameters)
        self.assertEqual(stages[ops.FACE][0], "accepted")
        self.assertEqual(stages[ops.UPSCALE], ("rejected", ["identity_changed"]))

    def test_a_step_that_leaves_the_upload_below_the_restoration_limit_is_refused_though_the_restored_face_is_fine(self):
        """The restoration scores 0.70 against the upload; a later step that scores 0.60 against it could never leave a photograph the final check accepts, whatever it scores against the restored face."""
        parameters, _ = self.run_engine(upload_similarity=[0.70, 0.60], restored_similarity=0.99)
        stages = self.stages(parameters)
        self.assertEqual(stages[ops.FACE][0], "accepted")
        self.assertEqual(stages[ops.UPSCALE], ("rejected", ["identity_changed"]))

    def test_the_setting_defaults_to_on(self):
        self.assertIs(settings.ADAPTIVE_BASELINE_AFTER_FACE_RESTORATION, True)

    def test_the_steps_before_a_restoration_are_unaffected(self):
        """Nothing is restored, so the validator keeps the upload's own judge (no second embedding, no baseline in any verdict)."""
        sharp = base.portrait(face_box=BOX)
        built = []

        class Identity(Judge):
            def __init__(self, source, allowed):
                super().__init__(0.97)
                built.append(source)

        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=BiasedDetector(BOX, 0.0, sharp.shape[1])), config=EngineConfig(), runners={ops.UPSCALE: clear_enlargement})
        with patch("enhancement.adaptive.service.IdentityChecker", Identity), override_settings(**ENGINE_SETTINGS):
            _out, _level, parameters = service.enhance(sharp, style="ai_max")
        self.assertEqual(len(built), 1)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [ops.UPSCALE])

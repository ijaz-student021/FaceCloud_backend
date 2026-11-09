"""After a lift is accepted, a later step whose face geometry the checks against the ORIGINAL refuse gets a second opinion against the lit image it was given (ADAPTIVE_BASELINE_AFTER_LIGHTING).

The library's 29.jpg (a 293 px photograph, the lift and the face restoration accepted): the enlargement read 0.073 against the original's landmarks (limit 0.06) and 0.046 beyond a plain enlargement
of the DARK original (limit 0.035), so the lit photograph kept its size; against the image it was given it read 0.042 (docs/evaluation/2026-11-06-enlargement-after-lift.md). The detector is a stand-in
here (CI has no weights): these tests are about the decision rule, not about the detector. All distances are in pixels of the original photograph; a face is 114 px wide (SHARE).
"""
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .adaptive.validator import FinalImageQualityValidator, Limits
from .test_adaptive_enlargement_geometry import BiasedDetector, clear_enlargement
from .test_adaptive_landmark_floor import VERIFIED, face
from .test_adaptive_polish import _no_identity

BOX = (230, 100, 114, 179)
SHARE = 114.0
FACE = (75, 50, 90, 110)                 # the face of the 320x240 photograph of the engine tests below


class TwoSources:
    """The detector on a plain enlargement of the DARK original (pixels 0) and of the LIT image the step was given (pixels 128): different faces, as the real detector's are. Records what it was asked."""

    def __init__(self, dark_dx, lit_dx):
        self.dark_dx, self.lit_dx, self.calls = dark_dx, lit_dx, []

    def analyze(self, image):
        lit, scale = image.mean() > 1, image.shape[1] / 640
        self.calls.append(("lit" if lit else "dark", image.shape[:2]))
        return SimpleNamespace(faces=[face(BOX, scale, dx=(self.lit_dx if lit else self.dark_dx) * scale)])


def scene(candidate_dx, scale=2, lit_dx=3.5, plain_dark_dx=3.1, plain_lit_dx=None, lifted=True, parent_scale=1, candidate_faces=None):
    """The state of the validator after a lift (``lifted``) for a step that made a candidate ``scale`` times the original, whose landmarks the detector put ``candidate_dx`` px from where it put the
    original's (dark: guesses, at 0), when the lit image the step was given (``parent_scale`` times the original) has them ``lit_dx`` px away, a plain enlargement of the dark original ``plain_dark_dx`` and
    of the lit image ``plain_lit_dx`` (default: where the lit image has them). Returns (validator, candidate, candidate_report, parent, parent_report, analyzer)."""
    original_report = SimpleNamespace(faces=[face(BOX, quality="poor")], width=640)
    parent_report = SimpleNamespace(faces=[face(BOX, parent_scale, dx=lit_dx * parent_scale)], width=640 * parent_scale)
    candidate_report = SimpleNamespace(faces=[face(BOX, scale, dx=candidate_dx * scale)] if candidate_faces is None else candidate_faces, width=640 * scale)
    analyzer = TwoSources(plain_dark_dx, lit_dx if plain_lit_dx is None else plain_lit_dx)
    validator = FinalImageQualityValidator(analyzer, original_report, original=np.zeros((480, 640, 3), np.uint8))
    validator.lifted_input = lifted
    candidate = np.zeros((480 * scale, 640 * scale, 3), np.uint8)
    parent = np.full((480 * parent_scale, 640 * parent_scale, 3), 128, np.uint8)
    return validator, candidate, candidate_report, parent, parent_report, analyzer


def judge(operation, validator, candidate, candidate_report, parent, parent_report):
    verdict = {"reasons": [], "gates": {}}
    validator._identity_gate(verdict, candidate, candidate_report, operation, parent, parent_report)
    return verdict["gates"]["identity"], verdict["reasons"]


class SettingTests(SimpleTestCase):
    def test_the_default_and_the_setting(self):
        self.assertIs(settings.ADAPTIVE_BASELINE_AFTER_LIGHTING, True)
        self.assertTrue(Limits().baseline_after_lighting)
        with override_settings(ADAPTIVE_BASELINE_AFTER_LIGHTING=False):
            self.assertFalse(Limits().baseline_after_lighting)


class SecondOpinionTests(SimpleTestCase):
    def test_the_premise_against_the_dark_original_alone_the_enlargement_is_refused(self):
        """29.jpg: 8.3 px from the original's landmarks is 0.073 (limit 0.06), and 5.2 px beyond a plain enlargement of the dark original is 0.046 (limit 0.035)."""
        status, reasons = judge("super_resolution", *scene(8.3, lifted=False)[:5])
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertAlmostEqual(status["landmark_shift"], 8.3 / SHARE, places=3)
        self.assertAlmostEqual(status["landmark_shift_beyond_plain_enlargement"], 5.2 / SHARE, places=3)
        self.assertNotIn("landmark_shift_from_lifted_input", status)

    def test_after_a_lift_it_is_judged_against_the_lit_image_it_was_given(self):
        status, reasons = judge("super_resolution", *scene(8.3)[:5])
        self.assertTrue(status["passed"], status)
        self.assertEqual(reasons, [])
        self.assertAlmostEqual(status["landmark_shift_from_lifted_input"], (8.3 - 3.5) / SHARE, places=3)       # 0.042: inside the 0.06 an enlargement is allowed
        self.assertAlmostEqual(status["landmark_shift"], 8.3 / SHARE, places=3)                                  # the earlier readings are still recorded
        self.assertIn("landmark_shift_beyond_plain_enlargement", status)

    def test_a_step_that_really_moved_the_face_is_still_refused(self):
        """The lit image is the reference now, so a candidate whose landmarks are far from it fails the same way: 0.073 from the lit image, 0.064 beyond its plain enlargement."""
        status, reasons = judge("super_resolution", *scene(8.3, lit_dx=0.0, plain_lit_dx=1.0)[:5])
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertAlmostEqual(status["landmark_shift_from_lifted_input"], 8.3 / SHARE, places=3)
        self.assertAlmostEqual(status["landmark_shift_beyond_plain_enlargement_of_lifted_input"], 7.3 / SHARE, places=3)

    def test_the_plain_enlargement_of_the_lit_image_takes_the_detectors_own_placement_out(self):
        """9.1 px (0.080) is what the detector alone does to a small face on any enlargement: against the lit image's landmarks it is over the 0.06 limit, beyond its plain enlargement it is nothing."""
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(9.1, lit_dx=0.0, plain_lit_dx=9.1, plain_dark_dx=0.0)
        status, reasons = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertTrue(status["passed"], status)
        self.assertAlmostEqual(status["landmark_shift_from_lifted_input"], 9.1 / SHARE, places=3)
        self.assertEqual(status["landmark_shift_beyond_plain_enlargement_of_lifted_input"], 0.0)
        self.assertEqual(analyzer.calls.count(("lit", (960, 1280))), 1)

    def test_the_setting_switches_it_off(self):
        with override_settings(ADAPTIVE_BASELINE_AFTER_LIGHTING=False):
            validator, *rest = scene(8.3)
            status, reasons = judge("super_resolution", validator, *rest[:4])
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertNotIn("landmark_shift_from_lifted_input", status)

    def test_without_a_lift_there_is_no_second_opinion_and_the_lit_image_is_never_analysed(self):
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(8.3, lifted=False)
        status, _ = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertFalse(status["passed"])
        self.assertNotIn("lit", [kind for kind, _size in analyzer.calls])

    def test_it_is_never_stricter_than_before(self):
        """A step the checks against the original accept is accepted without a second opinion: nothing from the lit image is read."""
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(2.0, lit_dx=50.0)
        status, reasons = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertTrue(status["passed"])
        self.assertEqual(reasons, [])
        self.assertNotIn("landmark_shift_from_lifted_input", status)
        self.assertEqual(analyzer.calls, [])

    def test_a_step_the_plain_enlargement_of_the_original_accepts_keeps_that_reading(self):
        """The existing rule decides first; the second opinion is only for what it refused."""
        status, _ = judge("super_resolution", *scene(8.3, plain_dark_dx=7.0)[:5])
        self.assertTrue(status["passed"])
        self.assertNotIn("landmark_shift_from_lifted_input", status)

    def test_a_face_restoration_and_a_polish_are_never_relaxed(self):
        for operation in ("face_restoration", "polish"):
            with self.subTest(operation):
                validator, candidate, candidate_report, parent, parent_report, analyzer = scene(8.3)
                validator.identity = VERIFIED
                status, reasons = judge(operation, validator, candidate, candidate_report, parent, parent_report)
                self.assertFalse(status["passed"])
                self.assertIn("face_geometry_changed", reasons)
                self.assertNotIn("landmark_shift_from_lifted_input", status)
                self.assertEqual(analyzer.calls, [])

    def test_a_face_that_is_lost_is_still_a_loss(self):
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(8.3, candidate_faces=[])
        status, reasons = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertFalse(status["passed"])
        self.assertIn("face_lost", reasons)
        self.assertEqual(analyzer.calls, [])

    def test_a_step_of_the_size_of_its_input_is_compared_with_it_directly(self):
        """After an enlargement the next step (the detail layer) is judged on the enlarged grid: 0.057 from the dark original's landmarks (limit 0.04), 0.004 from the enlarged lit image it was given."""
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(6.5, scale=2, lit_dx=6.0, plain_dark_dx=0.0, parent_scale=2)
        status, reasons = judge("detail_restoration", validator, candidate, candidate_report, parent, parent_report)
        self.assertTrue(status["passed"], status)
        self.assertAlmostEqual(status["landmark_shift"], 6.5 / SHARE, places=3)
        self.assertAlmostEqual(status["landmark_shift_from_lifted_input"], 0.5 / SHARE, places=3)
        self.assertNotIn("lit", [kind for kind, _size in analyzer.calls])           # same size: there is no enlargement to take the detector's placement out of

    def test_the_other_steps_after_a_lift_keep_their_own_limit(self):
        """0.04 for a denoise or a detail layer, not the enlargement's 0.06: 6.0 px from the lit image is 0.053."""
        status, reasons = judge("denoising", *scene(9.5, scale=1, lit_dx=3.5, plain_dark_dx=0.0, parent_scale=1)[:5])
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertAlmostEqual(status["landmark_shift_from_lifted_input"], 6.0 / SHARE, places=3)

    def test_the_identity_check_still_decides_identity(self):
        different = SimpleNamespace(check=lambda _image, restoring=False: {"status": "failed", "similarity": 0.3, "threshold": 0.8})
        validator, *rest = scene(8.3)
        validator.identity = different
        status, reasons = judge("super_resolution", validator, *rest[:4])
        self.assertFalse(status["passed"])
        self.assertIn("identity_changed", reasons)

    def test_without_the_step_s_input_there_is_nothing_to_compare_with(self):
        validator, candidate, candidate_report, _parent, _parent_report, _analyzer = scene(8.3)
        verdict = {"reasons": [], "gates": {}}
        validator._identity_gate(verdict, candidate, candidate_report, "super_resolution")            # the earlier call shape: no parent, no parent report
        self.assertFalse(verdict["gates"]["identity"]["passed"])
        self.assertIn("face_geometry_changed", verdict["reasons"])

    def test_a_lit_image_with_no_face_gives_no_second_opinion(self):
        validator, candidate, candidate_report, parent, parent_report, _analyzer = scene(8.3)
        parent_report.faces = []
        status, reasons = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)

    def test_a_face_the_lit_image_has_somewhere_else_gives_no_second_opinion(self):
        """The lit image's face is not where the candidate's is (the detector found another one): nothing to compare, so the refusal stands instead of an error."""
        validator, candidate, candidate_report, parent, parent_report, _analyzer = scene(8.3)
        parent_report.faces = [face((BOX[0] + 300, BOX[1], BOX[2], BOX[3]), 1, dx=3.5)]
        status, reasons = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertFalse(status["passed"])
        self.assertIn("face_geometry_changed", reasons)
        self.assertNotIn("landmark_shift_from_lifted_input", status)

    def test_a_tiny_face_of_the_lit_image_does_not_veto_or_rescue(self):
        validator, candidate, candidate_report, parent, parent_report, _analyzer = scene(8.3)
        parent_report.faces = [face(BOX, 1, dx=3.5, quality="too_small")]
        status, reasons = judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)
        self.assertFalse(status["passed"])                                           # nothing but a face too small to judge: no reference

    def test_the_plain_enlargement_of_the_lit_image_is_analysed_once_per_input_and_size(self):
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(9.1, lit_dx=0.0, plain_lit_dx=9.1, plain_dark_dx=0.0)
        for _variant in range(3):                                                    # the step's variants share its input
            self.assertTrue(judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)[0]["passed"])
        self.assertEqual(analyzer.calls.count(("lit", (960, 1280))), 1)
        other_input = np.full((480, 640, 3), 200, np.uint8)                          # a later step was given another image: it is analysed again
        self.assertTrue(judge("super_resolution", validator, candidate, candidate_report, other_input, parent_report)[0]["passed"])
        self.assertEqual(analyzer.calls.count(("lit", (960, 1280))), 2)

    def test_a_candidate_of_another_size_has_its_own_plain_enlargement(self):
        validator, candidate, candidate_report, parent, parent_report, analyzer = scene(9.1, lit_dx=0.0, plain_lit_dx=9.1, plain_dark_dx=0.0)
        self.assertTrue(judge("super_resolution", validator, candidate, candidate_report, parent, parent_report)[0]["passed"])
        bigger = np.zeros((1440, 1920, 3), np.uint8)                                  # 3x the original: the plain enlargement kept for 2x is not its reference
        bigger_report = SimpleNamespace(faces=[face(BOX, 3, dx=9.1 * 3)], width=1920)
        self.assertTrue(judge("super_resolution", validator, bigger, bigger_report, parent, parent_report)[0]["passed"])
        self.assertEqual(analyzer.calls.count(("lit", (960, 1280))), 1)
        self.assertEqual(analyzer.calls.count(("lit", (1440, 1920))), 1)


FACE_SMALL = (75, 50, 90, 110)


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class ServiceTests(SimpleTestCase):
    """The service tells the validator when a lift was accepted, and not before or without one."""

    def run_engine(self, **options):
        source = cv2.resize(base.portrait(brightness=0.12), (320, 240))             # small and dark: the lift, then the enlargement
        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=BiasedDetector(FACE_SMALL, 0.0, 320)), config=EngineConfig(),
                                                  runners={ops.LOW_LIGHT: base.runner_lift, ops.UPSCALE: clear_enlargement})
        seen, real, validators = [], FinalImageQualityValidator.evaluate, []
        gate_inputs, real_gate = [], FinalImageQualityValidator._identity_gate

        def spy(validator, parent, candidate, operation, parent_report, *args, **kwargs):
            seen.append((operation, validator.lifted_input))
            if validator not in validators:
                validators.append(validator)
            return real(validator, parent, candidate, operation, parent_report, *args, **kwargs)

        def gate_spy(validator, verdict, candidate, candidate_report, operation, parent=None, parent_report=None):
            gate_inputs.append((operation, parent is not None, parent_report is not None))
            return real_gate(validator, verdict, candidate, candidate_report, operation, parent, parent_report)

        with (patch.object(FinalImageQualityValidator, "evaluate", spy), patch.object(FinalImageQualityValidator, "_identity_gate", gate_spy),
              patch("enhancement.adaptive.service.IdentityChecker", _no_identity)):
            _output, _level, parameters = service.enhance(source, **options)
        self.validators = validators                                                  # the validators of the run, to read their state when it is over
        return seen, parameters["adaptive"]["operations_applied"], gate_inputs

    def test_the_validator_is_told_after_the_lift_is_accepted_and_not_before(self):
        seen, applied, _gate_inputs = self.run_engine()
        self.assertIn(ops.LOW_LIGHT, applied)
        self.assertEqual([validator.lifted_input for validator in self.validators], [True])
        lift_calls = [flag for operation, flag in seen if operation == ops.LOW_LIGHT]
        later_calls = [flag for operation, flag in seen if operation != ops.LOW_LIGHT]
        self.assertTrue(lift_calls and not any(lift_calls))                          # the lift itself is judged against the original
        self.assertTrue(later_calls and all(later_calls), seen)

    def test_without_a_lift_it_stays_down(self):
        seen, applied, _gate_inputs = self.run_engine(lighting="light")
        self.assertNotIn(ops.LOW_LIGHT, applied)
        self.assertTrue(applied, "something else was accepted (the enlargement), which must not count as a lift")
        self.assertTrue(seen and not any(flag for _operation, flag in seen), seen)
        self.assertEqual([validator.lifted_input for validator in self.validators], [False])

    def test_the_geometry_gate_is_given_the_image_each_step_started_from_and_its_report(self):
        _seen, _applied, gate_inputs = self.run_engine()
        self.assertTrue(any(operation == ops.UPSCALE for operation, _parent, _report in gate_inputs), gate_inputs)
        self.assertTrue(all(parent and report for _operation, parent, report in gate_inputs), gate_inputs)

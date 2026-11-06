"""The engine's log says what each stage did and, when the validator refuses a candidate, which gate and what it read.

Found on 2026-11-06: a photograph came back at its own size and the worker log said only ``rejected_for: ["face_geometry_changed"]``; the numbers behind it (a landmark shift of 0.0734 against 0.06, and 0.0356
beyond a plain enlargement against 0.035: a miss by 0.0006) had to be reproduced by running the photograph again. The log now carries them, with the size of the photograph and of the result, and a line when each
stage starts (a slow model is no longer silent). Never pixels, paths or user data. The models are stand-ins here.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import test_adaptive_engine as base
from .adaptive import operations as ops
from .adaptive.analyzer import ImageQualityAnalyzer
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .test_adaptive_enlargement_geometry import BiasedDetector, clear_enlargement
from .test_adaptive_polish import _no_identity

LOGGER = "enhancement.adaptive.service"
FACE = (75, 50, 90, 110)


def brightening(image, engine, variant):
    """A model that adds six levels to every bright pixel (the library's 25.jpg sky): the enlargement is refused for its highlights alone."""
    enlarged, models = clear_enlargement(image, engine, variant)
    bright = (cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY) > 200)[..., None]
    return (enlarged.astype("int16") + 6 * bright).clip(0, 255).astype("uint8"), models


@override_settings(**{**base.ALL_ON, "ADAPTIVE_ALWAYS_ENHANCE": False, "ADAPTIVE_POLISH_ENABLED": False})
class RunLogTests(SimpleTestCase):
    def run_engine(self, runner, **settings_):
        source = cv2.resize(base.portrait(), (320, 240))
        source[:40] = 246
        service = AdaptiveImageEnhancementService(analyzer=ImageQualityAnalyzer(detector=BiasedDetector(FACE, 0.0, 320)), config=EngineConfig(), runners={ops.UPSCALE: runner})
        with override_settings(**settings_), patch("enhancement.adaptive.service.IdentityChecker", _no_identity), self.assertLogs(LOGGER, level="INFO") as logged:
            output, _level, _parameters = service.enhance(source)
        structured = [record.getMessage() for record in logged.records if record.getMessage().startswith(("adaptive_stage ", "adaptive_enhancement "))]      # the SKIPPED lines are plain text
        return output, [(message.split(" ", 1)[0], json.loads(message.split(" ", 1)[1])) for message in structured]

    def entry(self, lines, name):
        return [payload for prefix, payload in lines if prefix == name]

    def test_a_refused_enlargement_is_logged_with_the_gate_that_failed_and_what_it_read(self):
        _output, lines = self.run_engine(brightening, ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False)
        (run,) = self.entry(lines, "adaptive_enhancement")
        (refusal,) = run["refused"]
        self.assertEqual((refusal["operation"], refusal["variant"], refusal["reasons"]), (ops.UPSCALE, "x2", ["highlight_clipping_increased"]))
        self.assertEqual(list(refusal["failed"]), ["highlights"])
        self.assertGreater(refusal["failed"]["highlights"]["clipping_growth"], 3.0)                       # the reading, not only the name
        self.assertNotIn("passed", refusal["failed"]["highlights"])
        self.assertEqual(run["stages"][0]["rejected_for"], ["highlight_clipping_increased"])               # what was logged before is still logged

    def test_the_size_of_the_photograph_and_of_the_result_are_logged(self):
        output, lines = self.run_engine(clear_enlargement)
        (run,) = self.entry(lines, "adaptive_enhancement")
        self.assertEqual(run["size"], {"input": [320, 240], "output": [640, 480]})
        self.assertEqual(output.shape[:2], (480, 640))

    def test_a_refused_photograph_logs_the_size_it_kept(self):
        _output, lines = self.run_engine(brightening, ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False)
        (run,) = self.entry(lines, "adaptive_enhancement")
        self.assertEqual(run["size"], {"input": [320, 240], "output": [320, 240]})

    def test_nothing_refused_logs_an_empty_list(self):
        _output, lines = self.run_engine(clear_enlargement)
        (run,) = self.entry(lines, "adaptive_enhancement")
        self.assertEqual(run["refused"], [])

    def test_each_stage_logs_when_it_starts(self):
        _output, lines = self.run_engine(clear_enlargement)
        starts = self.entry(lines, "adaptive_stage")
        self.assertEqual([stage["operation"] for stage in starts], [ops.UPSCALE])
        self.assertEqual(starts[0]["variants"], ["x2"])
        self.assertEqual(starts[0]["engines"], ["realesrgan"])
        self.assertEqual(starts[0]["megapixels"], 0.08)
        self.assertLess([prefix for prefix, _ in lines].index("adaptive_stage"), [prefix for prefix, _ in lines].index("adaptive_enhancement"))         # before the run's own line

    def test_the_lines_carry_no_pixels_or_paths(self):
        _output, lines = self.run_engine(brightening, ADAPTIVE_UPSCALE_HIGHLIGHT_GUARD=False)
        text = json.dumps(lines)
        self.assertNotIn("/", text.replace("\\/", ""))                  # no path
        self.assertLess(len(text), 4000)                                # compact: numbers, not arrays of pixels

    def test_a_retry_that_passes_is_not_listed_as_refused_and_the_first_is(self):
        """The highlight guard's second candidate passed: only the first (refused for its highlights) is in the list."""
        _output, lines = self.run_engine(brightening)
        (run,) = self.entry(lines, "adaptive_enhancement")
        self.assertEqual([item["variant"] for item in run["refused"]], ["x2"])
        self.assertEqual(run["operations"], [ops.UPSCALE])


class LogNeverBreaksARunTests(SimpleTestCase):
    def test_a_reading_that_is_not_plain_json_is_logged_not_raised(self):
        """A numpy number in a gate's reading must not stop the enhancement it describes."""
        candidate = SimpleNamespace(operation=ops.UPSCALE, variant="x2", verdict={"accepted": False, "reasons": ["face_geometry_changed"],
                                                                                 "gates": {"identity": {"passed": np.False_, "landmark_shift": np.float32(0.0734)}}})
        pool = SimpleNamespace(items=[candidate, SimpleNamespace(operation="original", variant="", verdict=None)])
        parameters = {"adaptive": {"outcome": "partially_enhanced", "operations_applied": [], "seconds": 1.0, "stages": [], "operations_skipped": [], "unresolved_defects": []},
                      "resolution_contract": {"source_dimensions": [448, 296], "actual_output_dimensions": [448, 296]}}
        report = SimpleNamespace(width=448, height=296, face_count=1, conditions=[])
        with self.assertLogs(LOGGER, level="INFO") as logged:
            AdaptiveImageEnhancementService._log_run(parameters, report, {"state": "verified"}, pool)
        run = json.loads(logged.records[0].getMessage().split(" ", 1)[1])
        (refusal,) = run["refused"]
        self.assertEqual(refusal["reasons"], ["face_geometry_changed"])
        self.assertAlmostEqual(float(refusal["failed"]["identity"]["landmark_shift"]), 0.0734, places=4)

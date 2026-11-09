"""Saving a gallery edit as a new version: the models run once, nothing is read that is not needed, and the polling client can see how far it is.

What is proved here: a guard that asks for a weaker render does not run the models again (and the weaker render is the model result blended back toward
the source); the tone controls keep their ten attempts; a neutral edit runs no model and no guard loop; the unenhanced baseline is decoded only for the
intensity slider; the timings are recorded in the edit's metadata; and ``stage`` / ``progress_percent`` on ``GET /api/enhancements/edits/{id}/``.
The queue for edits is tested with the other Celery routes in ``config/test_production_guard.py``.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import model_controls
from .edit_instrument import EditInstrument, NoInstrument
from .models import ImageEditVersion
from .services import _render_editor_controls, analyze_image_quality, apply_gallery_adjustments, process_enhancement_result
from .tasks import process_gallery_edit
from .test_balanced_policy import dark_photo
from .test_pipeline_regressions import NO_MODELS, ResultFixture, textured

OFF = dict(RETINEXFORMER_ENABLED=False, ZERO_DCE_ENABLED=False, NAFNET_DENOISE_ENABLED=False, NAFNET_DEBLUR_ENABLED=False, CODEFORMER_ENABLED=False)


def counting_runners():
    """Stand-ins for the two classical engines that count how often each ran; ``(runners, calls)``."""
    calls = {"opencv_gamma": 0, "opencv_unsharp": 0}

    def gamma(image):
        calls["opencv_gamma"] += 1
        return cv2.add(image, 40), {"provider": "stand_in"}

    def unsharp(image):
        calls["opencv_unsharp"] += 1
        return cv2.addWeighted(image, 1.5, cv2.GaussianBlur(image, (0, 0), 1.2), -0.5, 0), {"provider": "stand_in"}

    return {"opencv_gamma": gamma, "opencv_unsharp": unsharp}, calls


def guards_refuse(attempts):
    """A stand-in for ``analyze_image_quality`` that reports blown highlights for the first ``attempts`` renders after the one measuring the source."""
    seen = {"calls": 0}

    def measure(image):
        seen["calls"] += 1
        quality = analyze_image_quality(image)
        if 2 <= seen["calls"] <= 1 + attempts:
            quality = {**quality, "highlight_clipping_percent": 99.0}
        return quality

    return measure


@override_settings(**OFF)
class ModelsRunOncePerSaveTests(SimpleTestCase):
    def setUp(self):
        model_controls.clear_cache()
        self.addCleanup(model_controls.clear_cache)

    def render(self, source, controls, refusals):
        runners, calls = counting_runners()
        with patch.dict(model_controls._RUNNERS, runners), patch("enhancement.services.analyze_image_quality", guards_refuse(refusals)):
            model_controls.clear_cache()
            output, metadata = apply_gallery_adjustments(source, "manual", controls)
        return output, metadata, calls

    def test_the_guard_loop_does_not_run_the_models_again(self):
        _output, metadata, calls = self.render(dark_photo(), {"lowlight": 60, "deblur": 50, "contrast": 10}, refusals=3)
        self.assertEqual(len(metadata["guard_reductions"]), 3)                           # three weaker renders were tried...
        self.assertEqual(metadata["safety_strength"], round(0.78 ** 3, 4))
        self.assertEqual(calls, {"opencv_gamma": 1, "opencv_unsharp": 1})                # ...and each model ran exactly once

    def test_the_models_also_run_once_when_the_guard_never_passes(self):
        _output, metadata, calls = self.render(dark_photo(), {"lowlight": 60, "contrast": 10}, refusals=50)
        self.assertEqual(len(metadata["guard_reductions"]), 10)
        self.assertEqual(calls["opencv_gamma"], 1)

    def test_a_render_that_passes_first_time_is_the_same_image_as_before(self):
        source = dark_photo()
        controls = {"lowlight": 60, "deblur": 50, "contrast": 10}
        output, metadata, _calls = self.render(source, controls, refusals=0)
        runners, _ = counting_runners()
        with patch.dict(model_controls._RUNNERS, runners):
            model_controls.clear_cache()
            old_way = _render_editor_controls(source, controls, 1.0)[0]               # no model_stage: the models run inside, as they used to
        np.testing.assert_array_equal(output, old_way)
        self.assertEqual(metadata["guard_reductions"], [])

    def test_a_weaker_render_is_the_model_result_blended_back_toward_the_source(self):
        source = dark_photo()
        controls = {"lowlight": 60, "contrast": 10}
        output, metadata, _calls = self.render(source, controls, refusals=3)
        strength = 0.78 ** 3
        runners, _ = counting_runners()
        with patch.dict(model_controls._RUNNERS, runners):
            model_controls.clear_cache()
            old_way = _render_editor_controls(source, controls, strength)[0]          # the models run again at the reduced strength
        difference = np.abs(output.astype(np.int16) - old_way.astype(np.int16))
        self.assertLessEqual(int(difference.max()), 1)                                  # one model slider: the same image to within a grey level
        self.assertEqual(metadata["effective"]["lowlight"], round(60 * strength, 3))

    def test_the_report_reads_at_the_reduced_strength_as_it_did(self):
        _output, metadata, _calls = self.render(dark_photo(), {"lowlight": 60}, refusals=2)
        entry = metadata["model_controls"][0]
        strength = 0.78 ** 2
        self.assertEqual(entry["requested"], round(60 * strength, 2))
        self.assertEqual(entry["blend"], round(60 * strength / 100.0, 3))
        self.assertEqual((entry["control"], entry["engine"], entry["applied"]), ("lowlight", "opencv_gamma", True))

    def test_tone_controls_keep_all_ten_attempts_and_run_no_model(self):
        _output, metadata, calls = self.render(textured(), {"contrast": 20, "saturation": 10}, refusals=50)
        self.assertEqual(len(metadata["guard_reductions"]), 10)
        self.assertEqual(calls, {"opencv_gamma": 0, "opencv_unsharp": 0})
        self.assertEqual(metadata["model_controls"], [])

    def test_a_model_slider_that_could_not_run_is_still_reported_once(self):
        _output, metadata, _calls = self.render(textured(), {"face_restore": 80}, refusals=2)
        self.assertIn("face_restore_not_applied", metadata["fallback_reasons"])
        self.assertEqual(len([entry for entry in metadata["model_controls"] if entry["control"] == "face_restore"]), 1)


class InstrumentTests(SimpleTestCase):
    def test_stages_accumulate_and_the_total_is_included(self):
        times = iter([0.0, 1.0, 1.5, 2.0, 2.25, 10.0])           # created, stage a (1.0..1.5), stage a again (2.0..2.25), total
        instrument = EditInstrument(clock=lambda: next(times))
        with instrument.stage("a"):
            pass
        with instrument.stage("a"):
            pass
        self.assertEqual(instrument.timings_ms(), {"a": 750.0, "total": 10000.0})

    def test_progress_is_forwarded_and_the_default_instrument_records_nothing(self):
        seen = []
        instrument = EditInstrument(on_progress=lambda stage, percent: seen.append((stage, percent)))
        instrument.progress("enhancing", 40.9)
        self.assertEqual(seen, [("enhancing", 40)])
        quiet = NoInstrument()
        with quiet.stage("x"):
            quiet.progress("enhancing", 1)
            quiet.attempt_done(1.0)


@override_settings(**NO_MODELS)
class EditThroughTheTaskTests(ResultFixture):
    """The real endpoints, serializer and task body (the queue itself is mocked)."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.result = process_enhancement_result(self.make_result("edit.jpg", size=(240, 320)))
        self.url = f"/api/enhancements/results/{self.result.pk}/edits/"
        queued = patch("enhancement.views.process_gallery_edit.apply_async")
        queued.start()
        self.addCleanup(queued.stop)
        model_controls.clear_cache()
        self.addCleanup(model_controls.clear_cache)

    def make_edit(self, body):
        response = self.client.post(self.url, body, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        return response.json()["id"]

    def detail(self, edit_id):
        response = self.client.get(f"/api/enhancements/edits/{edit_id}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def run_edit(self, edit_id):
        self.assertEqual(process_gallery_edit.run(edit_id)["status"], "completed")
        return ImageEditVersion.objects.get(pk=edit_id)

    # -- E: nothing to do, nothing done -------------------------------------------------------------------------------
    def test_a_neutral_edit_runs_no_model_no_guard_loop_and_reads_no_baseline(self):
        edit_id = self.make_edit({"mode": "manual"})
        with patch("enhancement.services.apply_model_controls", side_effect=AssertionError("a model ran")), \
                patch("enhancement.services._read_preprocessed_baseline", side_effect=AssertionError("the baseline was decoded")):
            edit = self.run_edit(edit_id)
        self.assertEqual(edit.quality_metadata["outcome"], "no_change")
        self.assertEqual(edit.quality_metadata["guard_attempts"], {"count": 0, "ms": []})
        self.assertEqual(edit.quality_metadata["model_controls"], [])
        self.assertNotIn("read_baseline", edit.quality_metadata["timings_ms"])

    def test_the_baseline_is_decoded_only_for_the_intensity_slider(self):
        for body, expected in (
            ({"mode": "manual", "adjustments": {"contrast": 20}}, 0),
            ({"mode": "auto", "intent": "brighten"}, 0),
            ({"mode": "manual", "adjustments": {"contrast": 20, "intensity": 40}}, 1),
        ):
            with self.subTest(body=body):
                ImageEditVersion.objects.all().delete()
                edit_id = self.make_edit(body)
                with patch("enhancement.services._read_preprocessed_baseline", wraps=__import__("enhancement.services", fromlist=["x"])._read_preprocessed_baseline) as read:
                    edit = self.run_edit(edit_id)
                self.assertEqual(read.call_count, expected)
                self.assertEqual("read_baseline" in edit.quality_metadata["timings_ms"], bool(expected))

    # -- A: where the time went ---------------------------------------------------------------------------------------
    def test_the_timings_are_recorded_with_the_image_size_and_the_attempts(self):
        edit = self.run_edit(self.make_edit({"mode": "manual", "adjustments": {"contrast": 20, "saturation": 10}}))
        metadata = edit.quality_metadata
        timings = metadata["timings_ms"]
        for stage in ("read_source", "prepare", "analyze_before", "tone_render", "analyze_after", "restore_alpha", "encode", "verify", "save_file", "total"):
            self.assertIn(stage, timings)
            self.assertGreaterEqual(timings[stage], 0)
        self.assertGreaterEqual(timings["total"], max(value for key, value in timings.items() if key != "total"))
        self.assertEqual(metadata["image_pixels"], edit.output_width * edit.output_height)
        self.assertEqual(metadata["guard_attempts"]["count"], 1)
        self.assertEqual(len(metadata["guard_attempts"]["ms"]), 1)

    def test_each_model_slider_has_its_own_timing(self):
        runners, _calls = counting_runners()
        with override_settings(**OFF), patch.dict(model_controls._RUNNERS, runners):
            edit = self.run_edit(self.make_edit({"mode": "manual", "adjustments": {"lowlight": 50, "deblur": 40}}))
        timings = edit.quality_metadata["timings_ms"]
        self.assertIn("model_lowlight", timings)
        self.assertIn("model_deblur", timings)
        self.assertNotIn("model_denoise", timings)

    def test_the_old_metadata_is_all_still_there(self):
        edit = self.run_edit(self.make_edit({"mode": "manual", "adjustments": {"contrast": 20}}))
        for key in ("mode", "renderer_version", "outcome", "requested", "validated", "effective", "applied", "safety_strength", "safety_reasons",
                    "guard_reductions", "fallback_reasons", "model_controls", "quality_before", "quality_after", "dimensions_preserved",
                    "intensity", "encoded_output", "transparency", "source_version"):
            self.assertIn(key, edit.quality_metadata)

    # -- D: how far it is ---------------------------------------------------------------------------------------------
    def test_a_waiting_edit_says_queued_at_zero(self):
        body = self.detail(self.make_edit({"mode": "manual", "adjustments": {"contrast": 20}}))
        self.assertEqual((body["status"], body["stage"], body["progress_percent"]), ("queued", "queued", 0))

    def test_a_rendering_edit_says_where_it_is_and_never_goes_backwards(self):
        edit_id = self.make_edit({"mode": "manual", "adjustments": {"lowlight": 50, "deblur": 40, "contrast": 10}})
        seen = []
        real = __import__("enhancement.services", fromlist=["x"]).apply_gallery_adjustments
        runners, _calls = counting_runners()

        def look_while_rendering(*args, **kwargs):
            body = self.detail(edit_id)
            seen.append((body["status"], body["stage"], body["progress_percent"]))
            return real(*args, **kwargs)

        updates = []
        original_set = __import__("enhancement.services", fromlist=["x"])._set_edit_progress

        def record(edit_pk, token, stage, percent):
            updates.append((stage, percent))
            original_set(edit_pk, token, stage, percent)
            body = self.detail(edit_pk)
            seen.append((body["status"], body["stage"], body["progress_percent"]))

        with override_settings(**OFF), patch.dict(model_controls._RUNNERS, runners), \
                patch("enhancement.services.apply_gallery_adjustments", side_effect=look_while_rendering), \
                patch("enhancement.services._set_edit_progress", side_effect=record):
            self.run_edit(edit_id)
        self.assertEqual(seen[0], ("processing", "enhancing", 15))                       # the render started: the source was read
        stages = [stage for stage, _percent in updates]
        self.assertEqual(stages[0], "enhancing")
        self.assertEqual(stages[-2:], ["finishing", "finishing"])
        percents = [percent for _stage, percent in updates]
        self.assertEqual(percents, sorted(percents))                                    # never backwards
        self.assertTrue(all(5 <= percent <= 95 for percent in percents))
        self.assertTrue(all(status == "processing" and stage in ("reading", "enhancing", "finishing") for status, stage, _p in seen))
        self.assertGreaterEqual(len({percent for percent in percents}), 4)               # reading, each model, the tone render, encoding, saved

    def test_a_finished_edit_has_no_stage_and_is_at_one_hundred(self):
        edit_id = self.make_edit({"mode": "manual", "adjustments": {"contrast": 20}})
        self.run_edit(edit_id)
        body = self.detail(edit_id)
        self.assertEqual((body["status"], body["stage"], body["progress_percent"]), ("completed", None, 100))

    def test_a_failed_edit_has_neither(self):
        edit_id = self.make_edit({"mode": "manual", "adjustments": {"contrast": 20}})
        with patch("enhancement.services.apply_gallery_adjustments", side_effect=RuntimeError("boom")):
            self.assertEqual(process_gallery_edit.run(edit_id)["status"], "failed")
        body = self.detail(edit_id)
        self.assertEqual((body["status"], body["stage"], body["progress_percent"]), ("failed", None, None))

    def test_every_field_the_client_already_reads_is_still_there(self):
        body = self.detail(self.make_edit({"mode": "manual", "adjustments": {"contrast": 20}}))
        old_fields = {
            "id", "enhancement_result", "mode", "requested_adjustments", "applied_adjustments", "status", "output_name", "output_format", "output_width",
            "output_height", "output_file_size", "processing_duration_ms", "task_id", "quality_metadata", "failure_reason", "is_current", "download_url",
            "started_at", "finished_at", "created_at", "updated_at", "source_version", "from_preview", "advisor_recommendation",
        }
        self.assertTrue(old_fields <= set(body))
        self.assertEqual(set(body) - old_fields, {"stage", "progress_percent"})

    def test_a_progress_write_that_fails_never_fails_the_edit(self):
        from django.db.utils import OperationalError

        edit_id = self.make_edit({"mode": "manual", "adjustments": {"contrast": 20}})
        real_filter = ImageEditVersion.objects.filter

        class Broken:
            def update(self, **_kwargs):
                raise OperationalError("the connection dropped for a moment")

        def filter_that_breaks_progress(*args, **kwargs):
            if kwargs.get("claim_token") and kwargs.get("status") == "processing":
                return Broken()
            return real_filter(*args, **kwargs)

        with patch.object(ImageEditVersion.objects, "filter", side_effect=filter_that_breaks_progress):
            edit = self.run_edit(edit_id)
        self.assertEqual(edit.status, "completed")

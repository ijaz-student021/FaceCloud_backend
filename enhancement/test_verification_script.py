"""The adaptive API verification script reports honestly: a refused photograph is an expected rejection, never an enhancement, and the evidence holds no photograph.

The script itself needs the real models and photographs (see its docstring); these tests cover the parts that decide what it claims.
"""
import importlib.util
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_adaptive_api.py"
spec = importlib.util.spec_from_file_location("verify_adaptive_api_script", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

REFUSAL = "This photo was rejected by the checks, so it is not enhanced."


def completed(outcome):
    return {"status": "completed", "enhancement_outcome": outcome}


class ClassifyCaseTests(SimpleTestCase):
    def test_each_engine_outcome_is_reported_as_itself(self):
        for outcome in ("enhanced", "partially_enhanced", "original_preserved"):
            with self.subTest(outcome):
                self.assertEqual(script.classify_case(202, {"id": 1}, completed(outcome), "accepted"), outcome)

    def test_a_refused_rejected_photograph_is_an_expected_rejection_never_enhanced(self):
        result = script.classify_case(400, [REFUSAL], None, "rejected")
        self.assertEqual(result, "expected_rejection")
        self.assertNotIn(result, ("enhanced", "partially_enhanced", "original_preserved"))

    def test_a_400_for_any_other_reason_is_a_failure(self):
        self.assertEqual(script.classify_case(400, [REFUSAL], None, "accepted"), "failed")                      # refused, but the photo was not rejected
        self.assertEqual(script.classify_case(400, ["Some other validation error."], None, "rejected"), "failed")
        self.assertEqual(script.classify_case(400, {"detail": REFUSAL}, None, "rejected"), "failed")             # not the stock body

    def test_a_job_that_did_not_complete_or_an_unknown_outcome_is_a_failure(self):
        self.assertEqual(script.classify_case(202, {"id": 1}, {"status": "failed", "enhancement_outcome": "failed"}, "accepted"), "failed")
        self.assertEqual(script.classify_case(202, {"id": 1}, {"status": "processing"}, "accepted"), "failed")
        self.assertEqual(script.classify_case(202, {"id": 1}, completed("something_else"), "accepted"), "failed")
        self.assertEqual(script.classify_case(202, {"id": 1}, None, "accepted"), "failed")
        self.assertEqual(script.classify_case(500, {}, None, "accepted"), "failed")
        self.assertEqual(script.classify_case(201, {}, completed("enhanced"), "accepted"), "failed")             # only 202 means "queued and done"

    def test_the_five_labels_are_exactly_the_reportable_ones(self):
        self.assertEqual(set(script.CLASSIFICATIONS), {"enhanced", "partially_enhanced", "original_preserved", "failed", "expected_rejection"})


class EvidenceTests(SimpleTestCase):
    def test_redaction_removes_links_to_stored_photographs_at_any_depth(self):
        body = {"id": 3, "download_url": "http://testserver/api/x/download/", "items": [{"preview_url": "http://x", "keep": 1}], "keep": "yes"}
        redacted = script.redact(body)
        self.assertEqual(redacted["download_url"], "<redacted>")
        self.assertEqual(redacted["items"][0], {"preview_url": "<redacted>", "keep": 1})
        self.assertEqual((redacted["id"], redacted["keep"]), (3, "yes"))
        self.assertEqual(body["download_url"], "http://testserver/api/x/download/")                                # the original is not modified
        self.assertEqual(script.redact(None), None)
        self.assertEqual(script.redact({"download_url": None}), {"download_url": None})

    def test_trimming_keeps_the_decision_and_drops_only_the_bulky_reports(self):
        body = {"id": 1, "status": "completed", "enhancement_outcome": "enhanced", "download_url": "x", "applied_parameters": {
            "algorithm": "adaptive_v1+retinexformer", "identity_verification": {"verified": False, "mode": "limited_unverified"}, "warnings": ["w"],
            "stage_reports": [{"stage": "x"}], "quality_before": {"big": 1}, "quality_comparison": {"big": 2}, "render_policy": {"big": 3},
            "adaptive": {"outcome": "enhanced", "summary": "s", "stages": [], "operations_applied": ["a"], "quality_report": {"big": 4},
                         "final_quality_report": {"big": 5}, "candidates": [1, 2]}}}
        trimmed = script.trim_response(body)
        parameters = trimmed["applied_parameters"]
        self.assertEqual(parameters["identity_verification"], {"verified": False, "mode": "limited_unverified"})
        self.assertEqual(parameters["adaptive"]["outcome"], "enhanced")
        self.assertTrue(parameters["trimmed"])
        for dropped in ("quality_before", "quality_comparison", "render_policy"):
            self.assertNotIn(dropped, parameters)
        for dropped in ("quality_report", "final_quality_report", "candidates"):
            self.assertNotIn(dropped, parameters["adaptive"])
        self.assertEqual((trimmed["id"], trimmed["enhancement_outcome"]), (1, "enhanced"))                         # the rest of the response is intact
        self.assertIn("quality_before", body["applied_parameters"])                                              # the input is not modified
        self.assertEqual(script.trim_response(["The refusal."]), ["The refusal."])
        self.assertIsNone(script.trim_response(None))

    def test_identity_is_honest_only_when_verified_means_a_comparison_held(self):
        honest = script.identity_block_is_honest
        self.assertTrue(honest(None))
        self.assertTrue(honest({"verified": False, "state": "not_verifiable"}))
        self.assertTrue(honest({"verified": True, "state": "verified"}))
        self.assertTrue(honest({"verified": True, "state": "not_applicable"}))                                    # an unchanged photograph
        self.assertFalse(honest({"verified": True, "state": "not_verifiable"}))
        self.assertFalse(honest({"verified": True, "state": "uncertain"}))

    def test_summary_fails_when_any_case_failed_or_there_are_no_cases(self):
        rows = [{"case": "a", "classification": "enhanced", "all_checks_pass": True},
                {"case": "b", "classification": "expected_rejection", "all_checks_pass": True}]
        summary = script.summarise(rows)
        self.assertTrue(summary["all_checks_pass"])
        self.assertEqual(summary["by_classification"]["expected_rejection"], 1)
        self.assertEqual(summary["by_classification"]["enhanced"], 1)
        rows.append({"case": "c", "classification": "failed", "all_checks_pass": False})
        broken = script.summarise(rows)
        self.assertFalse(broken["all_checks_pass"])
        self.assertEqual(broken["failed_cases"], ["c"])
        self.assertFalse(script.summarise([])["all_checks_pass"])

    def test_hashes_are_sha256(self):
        self.assertEqual(script.sha256_bytes(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"abc")
            handle.flush()
            self.assertEqual(script.sha256_file(handle.name), script.sha256_bytes(b"abc"))

    def test_the_manifest_pins_the_files_the_script_reports(self):
        pins = script.manifest_pins()
        self.assertIn("yunet_s_dynamic.onnx", pins)
        self.assertEqual(len(pins["yunet_s_dynamic.onnx"]["sha256"]), 64)

    def test_the_recorded_command_keeps_the_options_but_not_the_folders_of_one_machine(self):
        recorded = script.command_for_evidence(["/home/me/FaceCloud/scripts/verify_adaptive_api.py", "--images", "/Users/me/Pictures/family", "--out",
                                                "/tmp/evidence", "--consent", "absent", "--label=run-2", "--images=/other/place"])
        self.assertEqual(recorded, ["verify_adaptive_api.py", "--images", "<folder_of_consented_photos>", "--out", "<evidence_dir>", "--consent", "absent",
                                    "--label=run-2", "--images=<folder_of_consented_photos>"])
        self.assertFalse(any("/Users/" in item or "/home/" in item for item in recorded))

    def test_the_environment_and_git_records_are_serialisable(self):
        json.dumps(script.environment_info(), default=str)
        info = script.git_info(script.ROOT)
        self.assertEqual(set(info), {"commit", "branch", "dirty"})

    def test_tiles_are_opt_in_in_the_documented_interface(self):
        self.assertIn("--save-tiles", script.__doc__)
        self.assertIn("keep them off GitHub", script.__doc__)

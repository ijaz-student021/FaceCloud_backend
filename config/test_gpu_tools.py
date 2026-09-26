"""The GPU-session helper scripts: manifest, preflight and stage comparison.

They live in ``scripts/`` (not an app), so they are loaded by path. Only their pure
logic is tested here; running the real models is what the GPU session itself is for.
"""
import contextlib
import hashlib
import importlib.util
import json
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from django.conf import settings
from django.test import SimpleTestCase

SCRIPTS = Path(settings.BASE_DIR) / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"gpu_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest_tool = load_script("model_manifest")
preflight = load_script("gpu_preflight")
evaluation = load_script("evaluate_gpu_stages")


def make_repo(files):
    """A throwaway repository root holding ``{relative path: bytes}``."""
    root = Path(tempfile.mkdtemp(prefix="facecloud-gpu-tools-"))
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


class ModelManifestTests(SimpleTestCase):
    FILES = {".runtime/models/a.pth": b"alpha", ".runtime/models/gfpgan/b.pth": b"beta", "gfpgan/weights/c.pth": b"gamma"}

    def test_a_manifest_records_every_file_with_size_and_checksum(self):
        manifest = manifest_tool.build_manifest(make_repo(self.FILES))
        self.assertEqual(sorted(manifest["roots"]["models"]), ["a.pth", "gfpgan/b.pth"])
        self.assertEqual(manifest["roots"]["models"]["a.pth"], {"size": 5, "sha256": hashlib.sha256(b"alpha").hexdigest()})
        self.assertEqual(list(manifest["roots"]["gfpgan_weights"]), ["c.pth"])

    def test_an_identical_copy_verifies_clean(self):
        manifest = manifest_tool.build_manifest(make_repo(self.FILES))
        result = manifest_tool.verify_manifest(make_repo(self.FILES), manifest)
        self.assertEqual((result["missing"], result["mismatched"], result["extra"]), ([], [], []))
        self.assertEqual(len(result["ok"]), 3)

    def test_missing_changed_and_extra_files_are_reported(self):
        manifest = manifest_tool.build_manifest(make_repo(self.FILES))
        copy = make_repo({".runtime/models/a.pth": b"ALPHA", ".runtime/models/new.pth": b"n"})
        result = manifest_tool.verify_manifest(copy, manifest)
        self.assertEqual(result["mismatched"], ["models/a.pth"])
        self.assertEqual(sorted(result["missing"]), ["gfpgan_weights/c.pth", "models/gfpgan/b.pth"])
        self.assertEqual(result["extra"], ["models/new.pth"])

    def test_the_command_line_exit_codes(self):
        repo = make_repo(self.FILES)
        path = repo / "manifest.json"

        def run(*args):
            with contextlib.redirect_stdout(io.StringIO()):
                return manifest_tool.main([*args, "--repo", str(repo), "--manifest", str(path)])

        self.assertEqual(run("write"), 0)
        self.assertEqual(run("verify"), 0)
        (repo / ".runtime/models/a.pth").write_bytes(b"tampered")
        self.assertEqual(run("verify"), 1)
        path.unlink()
        self.assertEqual(run("verify"), 2)

    def test_the_committed_manifest_is_well_formed(self):
        path = Path(settings.BASE_DIR) / "docs" / "gpu" / "model_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for files in manifest["roots"].values():
            for details in files.values():
                self.assertEqual(len(details["sha256"]), 64)
                self.assertGreater(details["size"], 0)


class PreflightJudgementTests(SimpleTestCase):
    def setUp(self):
        self.file = make_repo({"m.bin": b"weights"}) / "m.bin"
        self.sha = hashlib.sha256(b"weights").hexdigest()

    def test_a_present_file_with_the_right_checksum_passes(self):
        self.assertEqual(preflight.judge_model(self.file, self.sha, needed=True)[0], "PASS")

    def test_a_wrong_checksum_always_fails_even_when_the_stage_is_off(self):
        self.assertEqual(preflight.judge_model(self.file, "0" * 64, needed=False)[0], "FAIL")

    def test_a_missing_file_fails_when_needed_and_only_warns_when_the_stage_is_off(self):
        missing = self.file.parent / "gone.bin"
        self.assertEqual(preflight.judge_model(missing, self.sha, needed=True)[0], "FAIL")
        self.assertEqual(preflight.judge_model(missing, self.sha, needed=False)[0], "WARN")

    def test_an_unpinned_checksum_is_a_warning_and_verification_can_be_skipped(self):
        self.assertEqual(preflight.judge_model(self.file, "", needed=True)[0], "WARN")
        self.assertEqual(preflight.judge_model(self.file, "0" * 64, needed=True, verify=False)[0], "PASS")

    def test_the_worst_status_decides_the_result(self):
        _text, worst = preflight.render([("A", [("PASS", "x", "ok")]), ("B", [("WARN", "y", "hm")])])
        self.assertEqual(worst, "WARN")
        _text, worst = preflight.render([("A", [("WARN", "x", ""), ("FAIL", "y", "")])])
        self.assertEqual(worst, "FAIL")

    def test_every_model_the_pipeline_can_load_is_in_the_table(self):
        names = {row[0] for row in preflight.model_table(settings)}
        self.assertTrue({"zero_dce", "edsr", "purevision", "purescale", "realesrgan_hd", "nafnet_deblur",
                         "colorization", "gfpgan", "gfpgan_detector", "gfpgan_parser", "sface_identity"} <= names)

    def test_gfpgan_files_are_only_needed_when_it_is_switched_on(self):
        def needed(enabled):
            fake = SimpleNamespace(**{key: getattr(settings, key) for key in dir(settings) if key.isupper()})
            fake.GFPGAN_ENABLED = enabled
            return {name: flag for name, flag, _path, _sha in preflight.model_table(fake)}

        on, off = needed(True), needed(False)
        self.assertTrue(on["gfpgan"] and on["gfpgan_detector"] and on["gfpgan_parser"])
        self.assertFalse(off["gfpgan"] or off["gfpgan_detector"] or off["gfpgan_parser"])


class EvaluationLogicTests(SimpleTestCase):
    def record(self, seconds, noise, **extra):
        base = {"seconds": seconds, "noise": noise, "luminance": 90.0, "colourfulness": 5.0, "detail": 100.0, "stages": {}}
        base.update(extra)
        return base

    def test_configuration_names_are_validated(self):
        self.assertEqual(evaluation.parse_configs("baseline, hd"), ["baseline", "hd"])
        for bad in ("nope", "baseline,nope", " , "):
            with self.assertRaises(ValueError):
                evaluation.parse_configs(bad)

    def test_the_sample_is_reproducible_and_bounded(self):
        paths = [f"p{i}.png" for i in range(100)]
        self.assertEqual(evaluation.pick_sample(paths, 10, 11), evaluation.pick_sample(list(reversed(paths)), 10, 11))
        self.assertNotEqual(evaluation.pick_sample(paths, 10, 11), evaluation.pick_sample(paths, 10, 12))
        self.assertEqual(len(evaluation.pick_sample(paths[:5], 60, 1)), 5)

    def test_summaries_count_errors_visible_noise_and_stage_use(self):
        stages = {"purevision": {"applied": True, "fallback": ""}, "hd_restoration": {"applied": False, "fallback": "disabled"}}
        summary = evaluation.summarize([
            self.record(2.0, 6.0, stages=stages), self.record(4.0, 2.0, stages=stages), {"error": "boom"},
        ])
        self.assertEqual((summary["photos"], summary["errors"]), (3, 1))
        self.assertEqual(summary["mean_seconds"], 3.0)
        self.assertEqual(summary["share_visible_noise"], 0.5)
        self.assertEqual(summary["stages_applied"], {"purevision": 2, "hd_restoration": 0})
        self.assertEqual(summary["fallbacks"], {"hd_restoration": {"disabled": 2}})

    def test_a_configuration_that_failed_everywhere_still_summarises(self):
        self.assertEqual(evaluation.summarize([{"error": "x"}, {"error": "y"}]), {"photos": 2, "errors": 2})

    def test_identity_similarity_is_aggregated(self):
        guards = [{"status": "passed", "similarity": 0.9, "reason": ""}, {"status": "skipped", "similarity": None, "reason": "too dark"}]
        summary = evaluation.summarize([self.record(1.0, 1.0, identity=guard) for guard in guards])
        self.assertEqual(summary["identity_guard"]["statuses"], {"passed": 1, "skipped": 1})
        self.assertEqual(summary["identity_guard"]["min_similarity"], 0.9)
        self.assertEqual(summary["identity_guard"]["reasons"], {"too dark": 1})

    def test_comparison_against_the_baseline_uses_signed_differences(self):
        summaries = {
            "baseline": evaluation.summarize([self.record(2.0, 6.0)]),
            "hd": evaluation.summarize([self.record(5.0, 2.0, colourfulness=6.0)]),
        }
        deltas = evaluation.compare_to_baseline(summaries)
        self.assertEqual(deltas["hd"]["noise"], -4.0)
        self.assertEqual(deltas["hd"]["seconds"], 3.0)
        self.assertEqual(deltas["hd"]["colourfulness"], 1.0)
        self.assertEqual(evaluation.compare_to_baseline({"hd": summaries["hd"]}), {})

    def test_the_markdown_report_lists_configurations_fallbacks_and_deltas(self):
        summaries = {
            "baseline": evaluation.summarize([self.record(2.0, 6.0)]),
            "hd": evaluation.summarize([self.record(5.0, 2.0, stages={"hd_restoration": {"applied": False, "fallback": "model missing"}})]),
        }
        text = evaluation.markdown_report(
            summaries, evaluation.compare_to_baseline(summaries),
            {"run": "t", "photos": 1, "seed": 11, "face_photos": 0, "device": "CPU"},
        )
        for expected in ("| baseline |", "| hd |", "Change against baseline", "model missing"):
            self.assertIn(expected, text)

    def test_stage_and_identity_reports_tolerate_missing_or_odd_data(self):
        self.assertEqual(evaluation.stage_report({}), {})
        self.assertEqual(evaluation.stage_report({"purevision": "not a dict", "zero_dce": {"applied": 1}}),
                         {"zero_dce": {"applied": True, "fallback": ""}})
        self.assertIsNone(evaluation.identity_report({}))

    def test_degrading_a_photo_makes_it_small_and_keeps_it_deterministic(self):
        image = np.random.default_rng(0).integers(0, 255, size=(800, 600, 3), dtype=np.uint8)
        first, second = evaluation.degrade(image), evaluation.degrade(image)
        self.assertEqual(min(first.shape[:2]), 200)
        self.assertTrue(np.array_equal(first, second))
        self.assertEqual(evaluation.degrade(image, shortest_edge=300).shape[1], 300)
        tiny = np.zeros((50, 40, 3), dtype=np.uint8)
        self.assertEqual(evaluation.degrade(tiny).shape[:2], (50, 40))  # never enlarged

    def test_the_sample_can_be_exported_for_another_machine(self):
        source = make_repo({"data/a.png": b"a", "data/b.png": b"b", "faces/c.jpg": b"c"})
        destination = Path(tempfile.mkdtemp(prefix="facecloud-export-"))
        counts = evaluation.export_sample([source / "data/a.png", source / "data/b.png"], [source / "faces/c.jpg"], destination)
        self.assertEqual(counts, (2, 1))
        self.assertEqual(sorted(p.name for p in (destination / "images").iterdir()), ["a.png", "b.png"])
        self.assertEqual((destination / "faces" / "c.jpg").read_bytes(), b"c")
        self.assertEqual(evaluation.export_sample([], [], Path(tempfile.mkdtemp())), (0, 0))

    def test_every_configuration_only_sets_known_settings(self):
        for name, overrides in evaluation.CONFIGS.items():
            for key in overrides:
                self.assertTrue(hasattr(settings, key), f"{name}: {key} is not a setting")

"""The model manifest tool and the shared evaluation helpers.

They live in ``scripts/`` (not an app), so they are loaded by path. Only their pure
logic is tested here.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SCRIPTS = Path(settings.BASE_DIR) / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"model_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest_tool = load_script("model_manifest")
common = load_script("evaluation_common")

RETAINED_MODELS = ("face_recognition_sface_2021dec.onnx", "yunet_s_dynamic.onnx")


def make_repo(files):
    """A throwaway repository root holding ``{relative path: bytes}``."""
    root = Path(tempfile.mkdtemp(prefix="facecloud-model-tools-"))
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


class ModelManifestTests(SimpleTestCase):
    FILES = {".runtime/models/a.onnx": b"alpha", ".runtime/models/sub/b.onnx": b"beta"}

    def test_a_manifest_records_every_file_with_size_and_checksum(self):
        manifest = manifest_tool.build_manifest(make_repo(self.FILES))
        self.assertEqual(list(manifest["roots"]), ["models"])
        self.assertEqual(sorted(manifest["roots"]["models"]), ["a.onnx", "sub/b.onnx"])
        self.assertEqual(manifest["roots"]["models"]["a.onnx"], {"size": 5, "sha256": hashlib.sha256(b"alpha").hexdigest()})

    def test_an_identical_copy_verifies_clean(self):
        manifest = manifest_tool.build_manifest(make_repo(self.FILES))
        result = manifest_tool.verify_manifest(make_repo(self.FILES), manifest)
        self.assertEqual((result["missing"], result["mismatched"], result["extra"]), ([], [], []))
        self.assertEqual(len(result["ok"]), 2)

    def test_missing_changed_and_extra_files_are_reported(self):
        manifest = manifest_tool.build_manifest(make_repo(self.FILES))
        copy = make_repo({".runtime/models/a.onnx": b"ALPHA", ".runtime/models/new.onnx": b"n"})
        result = manifest_tool.verify_manifest(copy, manifest)
        self.assertEqual(result["mismatched"], ["models/a.onnx"])
        self.assertEqual(result["missing"], ["models/sub/b.onnx"])
        self.assertEqual(result["extra"], ["models/new.onnx"])

    def test_the_command_line_exit_codes(self):
        repo = make_repo(self.FILES)
        path = repo / "manifest.json"

        def run(*args):
            with contextlib.redirect_stdout(io.StringIO()):
                return manifest_tool.main([*args, "--repo", str(repo), "--manifest", str(path)])

        self.assertEqual(run("write"), 0)
        self.assertEqual(run("verify"), 0)
        (repo / ".runtime/models/a.onnx").write_bytes(b"tampered")
        self.assertEqual(run("verify"), 1)
        path.unlink()
        self.assertEqual(run("verify"), 2)

    def test_the_committed_manifest_lists_exactly_the_retained_models(self):
        path = Path(settings.BASE_DIR) / "docs" / "models" / "model_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(manifest["roots"]), ["models"])
        self.assertEqual(sorted(manifest["roots"]["models"]), sorted(RETAINED_MODELS))
        for details in manifest["roots"]["models"].values():
            self.assertEqual(len(details["sha256"]), 64)
            self.assertGreater(details["size"], 0)


class EvaluationHelpersTests(SimpleTestCase):
    def test_the_sample_is_reproducible_and_bounded(self):
        paths = [f"p{i}.png" for i in range(100)]
        self.assertEqual(common.pick_sample(paths, 10, 11), common.pick_sample(list(reversed(paths)), 10, 11))
        self.assertNotEqual(common.pick_sample(paths, 10, 11), common.pick_sample(paths, 10, 12))
        self.assertEqual(len(common.pick_sample(paths[:5], 60, 1)), 5)

    def test_identity_reports_tolerate_missing_data_and_carry_no_embedding(self):
        self.assertIsNone(common.identity_report({}))
        report = common.identity_report({"identity_guard": {
            "status": "preserved", "similarity": 0.9, "faces_compared": 1, "threshold": 0.4,
            "embedding": [0.1, 0.2], "fallback_reason": "x" * 300,
        }})
        self.assertEqual(report["status"], "preserved")
        self.assertNotIn("embedding", report)
        self.assertEqual(len(report["reason"]), 120)

    def test_a_degraded_copy_is_smaller_and_still_decodes(self):
        import numpy as np

        image = np.random.default_rng(1).integers(0, 255, (400, 300, 3), dtype=np.uint8)
        degraded = common.degrade(image, shortest_edge=150)
        self.assertEqual(degraded.shape[:2], (200, 150))

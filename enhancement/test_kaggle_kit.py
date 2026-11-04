"""The Kaggle GPU kit (scripts/kaggle_gpu/): what can be checked without Kaggle, a GPU or the models.

The scripts are not a package, so they are loaded by path. The Kaggle runner itself (``kernel/run_models.py``) has not been run on Kaggle; only its helpers are tested here.
"""
import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "scripts" / "kaggle_gpu"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("kaggle_prepare_inputs", KIT / "prepare_inputs.py")
bridge = load("kaggle_run", KIT / "kaggle_run.py")
runner = load("kaggle_run_models", KIT / "kernel" / "run_models.py")
scoring = load("kaggle_score_results", KIT / "score_results.py")


def write_png(path, value=100, size=64):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((size, size, 3), value, np.uint8))


class PrepareInputsTests(SimpleTestCase):
    def test_one_photograph_per_folder_comes_first_so_a_set_gives_different_people(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for person, count in (("anna", 3), ("bela", 2), ("cara", 1)):
                for index in range(count):
                    write_png(root / person / f"{person}_{index}.jpg")
            (root / "readme.txt").write_text("not a photograph")
            found = prepare.find_photos(root, 3)
            self.assertEqual([path.parent.name for path in found[:3]], ["anna", "bela", "cara"])

    def test_the_template_is_facexlibs_512_five_point_template(self):
        self.assertEqual(prepare.TEMPLATE.shape, (5, 2))
        self.assertAlmostEqual(float(prepare.TEMPLATE[0][0]), 192.98138, places=3)
        self.assertAlmostEqual(float(prepare.TEMPLATE[4][1]), 371.15118, places=3)

    def test_a_face_already_on_the_template_is_returned_as_it_is(self):
        image = np.random.default_rng(1).integers(0, 255, (512, 512, 3), dtype=np.uint8)
        aligned = prepare.align(image, prepare.TEMPLATE.copy())
        self.assertEqual(aligned.shape, (512, 512, 3))
        self.assertLess(float(np.abs(aligned.astype(int) - image.astype(int)).mean()), 1.0)

    def test_a_shifted_face_is_moved_onto_the_template(self):
        image = np.zeros((600, 600, 3), np.uint8)
        landmarks = prepare.TEMPLATE + np.array([40.0, 25.0], np.float32)
        for x, y in landmarks:
            cv2.circle(image, (int(x), int(y)), 6, (255, 255, 255), -1)
        aligned = prepare.align(image, landmarks)
        for x, y in prepare.TEMPLATE:
            self.assertGreater(int(aligned[int(round(y)), int(round(x))].mean()), 200, (x, y))

    def test_the_landmarks_are_the_five_pairs_after_the_box_in_the_order_the_template_uses(self):
        row = np.array([10, 20, 100, 120, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 0.99], np.float32)
        detector = SimpleNamespace(setInputSize=lambda size: None, detect=lambda image: (1, np.array([row, row * 0.5])))
        landmarks = prepare.detect_landmarks(detector, np.zeros((200, 200, 3), np.uint8))
        self.assertEqual(landmarks.tolist(), [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]])
        self.assertIsNone(prepare.detect_landmarks(SimpleNamespace(setInputSize=lambda size: None, detect=lambda image: (0, None)), np.zeros((9, 9, 3), np.uint8)))

    def test_every_degradation_keeps_the_size_and_changes_the_face(self):
        face = np.random.default_rng(2).integers(0, 255, (512, 512, 3), dtype=np.uint8)
        for kind in prepare.KINDS:
            degraded = prepare.degrade_face(kind, face)
            self.assertEqual((degraded.shape, degraded.dtype), (face.shape, np.uint8), kind)
            self.assertGreater(float(np.abs(degraded.astype(int) - face.astype(int)).mean()), 1.0, kind)
        np.testing.assert_array_equal(prepare.degrade_face("noise", face), prepare.degrade_face("noise", face))      # seeded: the same kit every time
        with self.assertRaises(ValueError):
            prepare.degrade_face("sepia", face)

    def test_the_kit_is_refused_inside_the_repository_and_for_unknown_models(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(prepare.main(["--photos", ".", "--out", str(ROOT / "kit")]), 2)
            self.assertIn("outside the repository", printed.getvalue())
            self.assertEqual(prepare.main(["--photos", ".", "--out", tempfile.gettempdir() + "/kit-x", "--models", "magic"]), 2)


class BridgeTests(SimpleTestCase):
    def test_the_kernel_is_a_private_gpu_script_with_internet_that_reads_the_kits_dataset(self):
        metadata = bridge.kernel_metadata("ali")
        self.assertEqual((metadata["id"], metadata["kernel_type"], metadata["code_file"]), ("ali/facecloud-face-bakeoff", "script", "run_models.py"))
        self.assertEqual((metadata["is_private"], metadata["enable_gpu"], metadata["enable_internet"]), ("true", "true", "true"))
        self.assertEqual(metadata["dataset_sources"], ["ali/facecloud-face-kit"])
        self.assertEqual(bridge.dataset_metadata("ali")["id"], "ali/facecloud-face-kit")

    def test_a_dry_run_prints_the_kaggle_commands_and_runs_none(self):
        with tempfile.TemporaryDirectory() as folder:
            kit = Path(folder) / "kit"
            kit.mkdir()
            (kit / "config.json").write_text(json.dumps({"ids": ["0000"], "kinds": ["blur"], "models": ["codeformer"]}))
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(bridge.main(["push", "--kit", str(kit), "--username", "ali", "--dry-run"]), 0)
            text = printed.getvalue()
            self.assertIn("kaggle datasets create", text)
            self.assertIn("kaggle kernels push", text)
            self.assertEqual(json.loads((kit / "dataset-metadata.json").read_text())["id"], "ali/facecloud-face-kit")
            self.assertTrue((Path(folder) / "kit_kernel" / "run_models.py").is_file())
            self.assertEqual(json.loads((Path(folder) / "kit_kernel" / "kernel-metadata.json").read_text())["enable_gpu"], "true")

    def test_a_folder_that_is_not_a_kit_is_refused(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(bridge.main(["push", "--kit", folder, "--username", "ali", "--dry-run"]), 2)
            self.assertIn("not a kit", printed.getvalue())

    def test_the_credential_check_never_reads_the_token(self):
        with tempfile.TemporaryDirectory() as home:
            from unittest.mock import patch

            with patch.object(Path, "home", return_value=Path(home)), patch.dict("os.environ", {}, clear=True):
                self.assertFalse(bridge.have_credentials())
                (Path(home) / ".kaggle").mkdir()
                (Path(home) / ".kaggle" / "kaggle.json").write_text("{}")
                self.assertTrue(bridge.have_credentials())


class RunnerHelperTests(SimpleTestCase):
    def test_the_removed_torchvision_module_is_replaced_in_every_file_that_imports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.py").write_text("from torchvision.transforms.functional_tensor import rgb_to_grayscale\n")
            (root / "sub").mkdir()
            (root / "sub" / "b.py").write_text("x = 1\n")
            self.assertEqual(runner.patch_torchvision(root), 1)
            self.assertIn("torchvision.transforms.functional import", (root / "a.py").read_text())
            self.assertNotIn("functional_tensor", (root / "a.py").read_text())

    def test_collect_finds_each_id_whatever_the_tools_folder_and_suffix_and_prefers_the_restored_face(self):
        with tempfile.TemporaryDirectory() as folder:
            root, destination = Path(folder) / "tool", Path(folder) / "dest"
            write_png(root / "restored_faces" / "0000.png", 10)
            write_png(root / "cmp" / "0000.png", 20)
            write_png(root / "restored_faces" / "0001_00.png", 30)       # a tool that adds a face index
            self.assertEqual(runner.collect(root, destination, ["0000", "0001", "0002"]), 2)
            self.assertEqual(int(cv2.imread(str(destination / "0000.png")).mean()), 10)
            self.assertEqual(int(cv2.imread(str(destination / "0001.png")).mean()), 30)
            self.assertFalse((destination / "0002.png").exists())

    def test_commands_run_without_a_shell(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as folder, patch.object(runner, "LOG", Path(folder) / "log.txt"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.sh(["python3", "-c", "print('listed')"]), 0)
            self.assertEqual(runner.sh("echo a && echo b"), 0)          # a string is split into arguments: the && is text for echo, not a second command
            log = (Path(folder) / "log.txt").read_text()
        self.assertIn("listed", log)
        self.assertIn("a && echo b", log)
        self.assertNotIn("shell=True", (KIT / "kernel" / "run_models.py").read_text().replace("with NO shell", ""))

    def test_the_models_in_the_kits_config_are_the_ones_the_runner_knows(self):
        self.assertEqual(set(prepare.MODELS), set(runner.MODELS))


class ScoringTests(SimpleTestCase):
    def test_summary_ignores_missing_values_and_the_table_prints_a_dash_for_them(self):
        self.assertEqual(scoring.summarise([0.8, None, 0.6]), (0.7, 0.6))
        self.assertEqual(scoring.summarise([None]), (None, None))
        row = {"model": "m", "kind": "blur", "found": 3, "expected": 4, "to_clean": [0.9, 0.7], "to_degraded": [], "detail": [1.0], "lpips": [], "seconds": None}
        table = scoring.table([row])
        self.assertIn("| blur | m | 3/4 | 0.80 / 0.70 | - | 1.00 | - | - |", table)

    def test_face_detail_is_higher_for_a_sharper_face(self):
        rng = np.random.default_rng(3)
        sharp = rng.integers(0, 255, (512, 512, 3), dtype=np.uint8)
        self.assertGreater(scoring.face_detail(sharp), scoring.face_detail(cv2.GaussianBlur(sharp, (0, 0), 3.0)) * 5)

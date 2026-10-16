"""EDSR is refused unless the file matches its pinned checksum (audit P2-16)."""
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import services


class EdsrIntegrityTests(SimpleTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="edsr-integrity-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        services._SUPERRES_CACHE.clear()
        self.addCleanup(services._SUPERRES_CACHE.clear)

    def model_file(self, content=b"not the published EDSR weights"):
        path = self.folder / "EDSR_x2.pb"
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    def test_a_file_that_does_not_match_is_refused_before_loading(self):
        path, _digest = self.model_file()
        with patch.object(services.cv2.dnn_superres, "DnnSuperResImpl_create") as create:
            with self.assertRaisesRegex(RuntimeError, "integrity verification"):
                services._load_edsr_model(path, 2)
        create.assert_not_called()

    def test_a_matching_file_loads_once_and_is_reused(self):
        path, digest = self.model_file()
        with override_settings(AI_RESTORATION_EDSR_MODEL_SHA256=digest), \
                patch.object(services.cv2.dnn_superres, "DnnSuperResImpl_create") as create:
            first = services._load_edsr_model(path, 2)
            second = services._load_edsr_model(path, 2)
        self.assertIs(first, second)
        create.assert_called_once()

    @override_settings(AI_RESTORATION_EDSR_MODEL_SHA256="")
    def test_a_missing_checksum_setting_is_refused(self):
        path, _digest = self.model_file()
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            services._load_edsr_model(path, 2)

    def test_the_pinned_value_is_the_manifest_value(self):
        manifest = json.loads((Path(settings.BASE_DIR) / "docs" / "gpu" / "model_manifest.json").read_text())

        def find(node):
            if isinstance(node, dict):
                if "EDSR_x2.pb" in node:
                    return node["EDSR_x2.pb"]
                return next((hit for hit in map(find, node.values()) if hit), None)
            return None

        self.assertEqual(find(manifest)["sha256"], settings.AI_RESTORATION_EDSR_MODEL_SHA256)

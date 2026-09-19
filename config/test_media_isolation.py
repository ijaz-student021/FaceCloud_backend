import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, override_settings


class MediaIsolationTests(SimpleTestCase):
    def test_tests_never_write_to_the_real_media_folder(self):
        real_media = Path(settings.BASE_DIR) / "media"
        self.assertNotEqual(Path(settings.MEDIA_ROOT).resolve(), real_media.resolve())
        self.assertTrue(
            Path(settings.MEDIA_ROOT).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()),
            settings.MEDIA_ROOT,
        )

    def test_the_default_storage_follows_the_isolated_folder(self):
        self.assertEqual(Path(default_storage.location).resolve(), Path(settings.MEDIA_ROOT).resolve())

    def test_a_suite_can_still_choose_its_own_folder(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            self.assertEqual(Path(default_storage.location).resolve(), Path(folder).resolve())

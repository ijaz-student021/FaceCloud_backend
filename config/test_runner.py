import shutil
import tempfile

from django.test import override_settings
from django.test.runner import DiscoverRunner


class FaceCloudTestRunner(DiscoverRunner):
    """Give every test run its own throwaway media folder.

    Tests that save files (uploads, enhanced images, previews) would otherwise
    write into the real MEDIA_ROOT, mixing debris into a developer's actual user
    files and colliding with real user ids. Suites that need their own folder can
    still ``override_settings(MEDIA_ROOT=...)`` on top of this.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._media_root = tempfile.mkdtemp(prefix="facecloud-test-media-")
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def teardown_test_environment(self, **kwargs):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().teardown_test_environment(**kwargs)

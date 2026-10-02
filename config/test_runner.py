import shutil
import tempfile
import unittest

from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.core.cache import cache
from django.test import override_settings
from django.test.runner import DiscoverRunner


class FastPasswordHasher(PBKDF2PasswordHasher):
    """PBKDF2 with a token iteration count, for tests only.

    At production strength one hash takes about 0.8 s, and almost every test
    creates users and logs in, so hashing dominated the suite's run time. The
    algorithm and code path are unchanged; only the work factor is lowered.
    """

    iterations = 1000


def _cache_clearing(result_class):
    """Wrap a unittest result class so each test starts with an empty cache.

    Throttle counters live in the cache, and a cache is not part of the
    transaction a test rolls back -- so without this they carry from one test
    into the next. That only shows up on some databases: DRF keys the throttle
    by ``request.user.pk``, PostgreSQL never reuses a primary key after a
    rolled-back transaction, but SQLite does. The same suite therefore passed on
    PostgreSQL and failed on SQLite, where several tests looked like one user
    and tripped an hourly limit.

    Clearing per test makes the suite behave the same on either engine, so it
    can be run without standing up PostgreSQL first.
    """

    class CacheClearingResult(result_class):
        def startTest(self, test):
            cache.clear()
            super().startTest(test)

    CacheClearingResult.__name__ = result_class.__name__
    return CacheClearingResult


class FaceCloudTestRunner(DiscoverRunner):
    """Make test runs fast and safe.

    * A fast password hasher (see ``FastPasswordHasher``).
    * An empty cache at the start of every test (see ``_cache_clearing``).
    * Image encryption switched on with a test key, for every test.
    * A throwaway media folder. Tests that save files (uploads, enhanced images,
      previews) would otherwise write into the real MEDIA_ROOT, mixing debris into
      a developer's actual user files and colliding with real user ids. Suites that
      need their own folder can still ``override_settings(MEDIA_ROOT=...)``.
    """

    def get_resultclass(self):
        # super() picks a class for --debug-sql and --pdb; keep whichever it
        # chose and only add the cache reset on top.
        return _cache_clearing(super().get_resultclass() or unittest.TextTestResult)

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._media_root = tempfile.mkdtemp(prefix="facecloud-test-media-")
        self._media_override = override_settings(
            MEDIA_ROOT=self._media_root,
            PASSWORD_HASHERS=["config.test_runner.FastPasswordHasher"],
            # Every test that saves or reads a file goes through the encrypting
            # storage, so any code that mishandles ciphertext fails loudly.
            IMAGE_ENCRYPTION_ENABLED=True,
            IMAGE_ENCRYPTION_KEY="test-only-image-encryption-key-0123456789",
            IMAGE_ENCRYPTION_KEY_ID=1,
            IMAGE_ENCRYPTION_PREVIOUS_KEYS={},
        )
        self._media_override.enable()

    def teardown_test_environment(self, **kwargs):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().teardown_test_environment(**kwargs)

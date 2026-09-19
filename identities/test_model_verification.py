import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from . import providers
from .providers import verify_model_checksum


class ModelVerificationCacheTests(SimpleTestCase):
    def setUp(self):
        providers._VERIFIED_MODELS.clear()
        self.addCleanup(providers._VERIFIED_MODELS.clear)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "model.onnx"
        self.path.write_bytes(b"model-weights-" * 1000)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def hashing_calls(self):
        return patch("identities.providers.hashlib.sha256", wraps=hashlib.sha256)

    def test_returns_the_correct_digest(self):
        self.assertEqual(verify_model_checksum(self.path, self.digest), self.digest)
        self.assertEqual(verify_model_checksum(self.path, self.digest.upper()), self.digest)

    def test_an_unchanged_file_is_hashed_once_not_on_every_call(self):
        with self.hashing_calls() as sha:
            for _ in range(50):
                verify_model_checksum(self.path, self.digest)
        self.assertEqual(sha.call_count, 1)

    def test_a_modified_file_is_hashed_again_and_rejected(self):
        verify_model_checksum(self.path, self.digest)
        # Same size, different content, and a clearly newer modification time.
        tampered = bytearray(self.path.read_bytes())
        tampered[0] ^= 0xFF
        self.path.write_bytes(bytes(tampered))
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

        with self.assertRaises(ValidationError):
            verify_model_checksum(self.path, self.digest)

    def test_a_restored_file_is_accepted_again(self):
        original = self.path.read_bytes()
        verify_model_checksum(self.path, self.digest)
        self.path.write_bytes(b"x" * len(original))
        with self.assertRaises(ValidationError):
            verify_model_checksum(self.path, self.digest)
        self.path.write_bytes(original)
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))

        self.assertEqual(verify_model_checksum(self.path, self.digest), self.digest)

    def test_failures_are_never_cached(self):
        with self.hashing_calls() as sha:
            for _ in range(3):
                with self.assertRaises(ValidationError):
                    verify_model_checksum(self.path, "0" * 64)
        self.assertEqual(sha.call_count, 3)

    def test_a_missing_expected_checksum_is_always_a_failure(self):
        for expected in ("", None):
            with self.subTest(expected=expected), self.assertRaises(ValidationError):
                verify_model_checksum(self.path, expected)

    def test_a_different_expected_checksum_is_not_satisfied_by_an_earlier_success(self):
        verify_model_checksum(self.path, self.digest)
        with self.assertRaises(ValidationError):
            verify_model_checksum(self.path, "f" * 64)

    def test_a_missing_file_still_raises(self):
        with self.assertRaises(OSError):
            verify_model_checksum(self.path.with_name("absent.onnx"), self.digest)

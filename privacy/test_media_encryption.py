import hashlib
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from config import image_crypto
from privacy.media_encryption import TEMPORARY_SUFFIX, encrypt_stored_media, media_names

KEY_ONE = "conversion-test-key-one-0123456789abc"
KEY_TWO = "conversion-test-key-two-0123456789abc"


def payload(seed, size=200_000):
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:size])


class ConversionTests(SimpleTestCase):
    """Each test gets its own empty media folder, so counts are exact whatever else ran."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=folder.name))
        self.created = []

    def write(self, name, data, *, encrypted):
        with override_settings(IMAGE_ENCRYPTION_ENABLED=encrypted):
            saved = default_storage.save(name, ContentFile(data))
        self.created.append(saved)
        return saved

    def path(self, name):
        return Path(default_storage.path(name))

    def read(self, name):
        with default_storage.open(name) as handle:
            return handle.read()

    def test_plaintext_files_are_encrypted_in_place_and_read_back_identically(self):
        first = self.write("users/7/originals/a.bin", payload(1), encrypted=False)
        second = self.write("users/7/enhanced/b.bin", payload(2, 70_000), encrypted=False)
        self.assertFalse(default_storage.is_encrypted(first))

        summary = encrypt_stored_media()

        self.assertEqual((summary["encrypted"], summary["failed"], summary["already_current"]), (2, 0, 0))
        for name, seed, size in ((first, 1, 200_000), (second, 2, 70_000)):
            with self.subTest(name=name):
                self.assertTrue(default_storage.is_encrypted(name))
                self.assertNotIn(payload(seed, 64), self.path(name).read_bytes())
                self.assertEqual(self.read(name), payload(seed, size))
                self.assertEqual(default_storage.size(name), size)

    def test_modification_time_is_preserved_so_the_orphan_grace_period_still_works(self):
        name = self.write("users/7/originals/old.bin", payload(3), encrypted=False)
        os.utime(self.path(name), (1_600_000_000, 1_600_000_000))

        encrypt_stored_media()

        self.assertEqual(int(self.path(name).stat().st_mtime), 1_600_000_000)

    def test_a_dry_run_reports_without_touching_anything(self):
        name = self.write("users/7/originals/dry.bin", payload(4), encrypted=False)
        before = self.path(name).read_bytes()

        summary = encrypt_stored_media(dry_run=True)

        self.assertEqual(summary["encrypted"], 1)
        self.assertEqual(self.path(name).read_bytes(), before)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        name = self.write("users/7/originals/twice.bin", payload(5), encrypted=False)
        encrypt_stored_media()
        after_first = self.path(name).read_bytes()

        summary = encrypt_stored_media()

        self.assertEqual((summary["encrypted"], summary["already_current"]), (0, 1))
        self.assertEqual(self.path(name).read_bytes(), after_first)

    def test_files_already_encrypted_under_the_current_key_are_left_alone(self):
        name = self.write("users/7/enhanced/new.bin", payload(6), encrypted=True)
        before = self.path(name).read_bytes()

        summary = encrypt_stored_media(rotate=True)

        self.assertEqual(summary["already_current"], 1)
        self.assertEqual(self.path(name).read_bytes(), before)

    def test_rotation_rewrites_files_under_the_old_key_only_when_asked(self):
        with override_settings(IMAGE_ENCRYPTION_KEY=KEY_ONE, IMAGE_ENCRYPTION_KEY_ID=1, IMAGE_ENCRYPTION_PREVIOUS_KEYS={}):
            name = self.write("users/7/enhanced/rot.bin", payload(7), encrypted=True)
        rotated_settings = dict(
            IMAGE_ENCRYPTION_KEY=KEY_TWO, IMAGE_ENCRYPTION_KEY_ID=2, IMAGE_ENCRYPTION_PREVIOUS_KEYS={1: KEY_ONE},
        )
        with override_settings(**rotated_settings):
            self.assertEqual(default_storage.key_id(name), 1)
            untouched = encrypt_stored_media()
            self.assertEqual((untouched["rotated"], untouched["already_current"]), (0, 1))
            self.assertEqual(default_storage.key_id(name), 1)

            summary = encrypt_stored_media(rotate=True)

            self.assertEqual(summary["rotated"], 1)
            self.assertEqual(default_storage.key_id(name), 2)
            self.assertEqual(self.read(name), payload(7))

    def test_a_bad_copy_never_replaces_the_original(self):
        name = self.write("users/7/originals/guard.bin", payload(8), encrypted=False)
        before = self.path(name).read_bytes()
        real = image_crypto.encrypt_chunks

        def corrupting(pieces, ring):
            first = True
            for piece in real(pieces, ring):
                yield (bytes([piece[0] ^ 1]) + piece[1:]) if not first else piece
                first = False

        with patch("privacy.media_encryption.image_crypto.encrypt_chunks", side_effect=corrupting):
            with self.assertLogs("privacy.media_encryption", level="ERROR"):
                summary = encrypt_stored_media()

        self.assertEqual((summary["failed"], summary["encrypted"]), (1, 0))
        self.assertEqual(self.path(name).read_bytes(), before)  # untouched plaintext
        self.assertFalse(Path(str(self.path(name)) + TEMPORARY_SUFFIX).exists())

    def test_unreferenced_files_are_converted_too_and_temporary_files_are_ignored(self):
        name = self.write("users/9/comparisons/orphan.jpg", payload(9), encrypted=False)
        stray = self.path(name).with_name("leftover.jpg" + TEMPORARY_SUFFIX)
        stray.write_bytes(b"partial")
        self.addCleanup(lambda: stray.unlink(missing_ok=True))

        self.assertNotIn(f"users/9/comparisons/{stray.name}", list(media_names()))
        encrypt_stored_media()

        self.assertTrue(default_storage.is_encrypted(name))

    def test_it_refuses_to_run_without_encryption_configured(self):
        with override_settings(IMAGE_ENCRYPTION_ENABLED=False):
            with self.assertRaises(RuntimeError):
                encrypt_stored_media()
            with self.assertRaises(CommandError):
                call_command("encrypt_stored_media", stdout=io.StringIO())

    def test_the_command_reports_and_supports_dry_run(self):
        self.write("users/7/originals/cmd.bin", payload(10), encrypted=False)

        dry = io.StringIO()
        call_command("encrypt_stored_media", "--dry-run", stdout=dry)
        self.assertIn("1 would be encrypted", dry.getvalue())

        real = io.StringIO()
        call_command("encrypt_stored_media", stdout=real)
        self.assertIn("1 were encrypted", real.getvalue())

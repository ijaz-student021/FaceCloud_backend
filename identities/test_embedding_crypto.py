import os
import subprocess
import sys

import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from .storage import (
    is_current_payload, payload_format, protect_embedding, reveal_embedding,
)

# Produced by the pre-AES cipher (FCID1) with IDENTITY_EMBEDDING_KEY
# "golden-fixture-key-v1" and the vector [0.25, -0.5, 0.75]. It must stay
# readable forever, or upgrading would strand existing biometric data.
LEGACY_KEY = "golden-fixture-key-v1"
LEGACY_PAYLOAD = bytes.fromhex(
    "4643494431d6cfb18adadd9361ceef20d497bd60d4f67062fd49d0d12d8b18bc76555247"
    "ad357687b9bca6039c9595f2808b76d3c6b253b77a6f382c9a29cad61c"
)
VECTOR = np.array([0.25, -0.5, 0.75], dtype=np.float32)

KEYS = dict(IDENTITY_EMBEDDING_KEY="current-secret-value-0123456789", IDENTITY_EMBEDDING_KEY_ID=1,
            IDENTITY_EMBEDDING_PREVIOUS_KEYS={})


@override_settings(**KEYS)
class AesGcmFormatTests(SimpleTestCase):
    def test_round_trip_uses_the_new_format_and_never_stores_plain_floats(self):
        protected = protect_embedding(VECTOR)
        self.assertEqual(protected[:5], b"FCID2")
        self.assertEqual(protected[5], 1)
        self.assertEqual(len(protected), 6 + 12 + VECTOR.nbytes + 16)
        self.assertNotIn(VECTOR.tobytes(), protected)
        np.testing.assert_array_equal(reveal_embedding(protected, 3), VECTOR)

    def test_every_protection_uses_a_fresh_nonce(self):
        self.assertNotEqual(protect_embedding(VECTOR), protect_embedding(VECTOR))

    def test_tampering_with_any_part_of_the_payload_is_rejected(self):
        protected = protect_embedding(VECTOR)
        for position, description in (
            (0, "format marker"), (5, "key id"), (8, "nonce"), (20, "ciphertext"), (-1, "tag"),
        ):
            tampered = bytearray(protected)
            tampered[position] ^= 0x01
            with self.subTest(part=description):
                with self.assertRaisesMessage(ValidationError, "protected identity embedding is invalid"):
                    reveal_embedding(bytes(tampered), 3)

    def test_truncated_and_foreign_payloads_are_rejected(self):
        protected = protect_embedding(VECTOR)
        for payload in (b"", b"FCID2", protected[:20], b"not-a-protected-embedding"):
            with self.subTest(length=len(payload)):
                with self.assertRaises(ValidationError):
                    reveal_embedding(payload, 3)

    def test_wrong_dimension_and_non_finite_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            reveal_embedding(protect_embedding(VECTOR), 4)
        with self.assertRaises(ValidationError):
            reveal_embedding(protect_embedding([1.0, float("nan"), 0.0]), 3)

    def test_a_different_secret_cannot_read_the_record(self):
        protected = protect_embedding(VECTOR)
        with override_settings(IDENTITY_EMBEDDING_KEY="some-other-secret-value-0123456789"):
            with self.assertRaises(ValidationError):
                reveal_embedding(protected, 3)

    def test_accepts_bytearray_and_memoryview_from_the_database_driver(self):
        protected = protect_embedding(VECTOR)
        np.testing.assert_array_equal(reveal_embedding(bytearray(protected), 3), VECTOR)
        np.testing.assert_array_equal(reveal_embedding(memoryview(protected), 3), VECTOR)

    @override_settings(IDENTITY_EMBEDDING_KEY_ID=0)
    def test_an_out_of_range_key_id_is_a_configuration_error(self):
        with self.assertRaises(ImproperlyConfigured):
            protect_embedding(VECTOR)
        with override_settings(IDENTITY_EMBEDDING_KEY_ID=256):
            with self.assertRaises(ImproperlyConfigured):
                protect_embedding(VECTOR)


class LegacyCompatibilityTests(SimpleTestCase):
    @override_settings(IDENTITY_EMBEDDING_KEY=LEGACY_KEY, IDENTITY_EMBEDDING_KEY_ID=1, IDENTITY_EMBEDDING_PREVIOUS_KEYS={})
    def test_a_real_legacy_payload_is_still_readable(self):
        np.testing.assert_array_equal(reveal_embedding(LEGACY_PAYLOAD, 3), VECTOR)
        self.assertEqual(payload_format(LEGACY_PAYLOAD), ("v1", None))
        self.assertFalse(is_current_payload(LEGACY_PAYLOAD))

    @override_settings(IDENTITY_EMBEDDING_KEY="brand-new-secret-value-0123456789", IDENTITY_EMBEDDING_KEY_ID=2,
                       IDENTITY_EMBEDDING_PREVIOUS_KEYS={1: LEGACY_KEY})
    def test_a_legacy_payload_is_found_under_a_previous_key(self):
        np.testing.assert_array_equal(reveal_embedding(LEGACY_PAYLOAD, 3), VECTOR)

    @override_settings(IDENTITY_EMBEDDING_KEY="brand-new-secret-value-0123456789", IDENTITY_EMBEDDING_KEY_ID=2,
                       IDENTITY_EMBEDDING_PREVIOUS_KEYS={})
    def test_a_legacy_payload_without_its_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            reveal_embedding(LEGACY_PAYLOAD, 3)

    @override_settings(IDENTITY_EMBEDDING_KEY=LEGACY_KEY, IDENTITY_EMBEDDING_KEY_ID=1, IDENTITY_EMBEDDING_PREVIOUS_KEYS={})
    def test_a_tampered_legacy_payload_is_rejected(self):
        tampered = bytearray(LEGACY_PAYLOAD)
        tampered[-1] ^= 0x01
        with self.assertRaises(ValidationError):
            reveal_embedding(bytes(tampered), 3)


class KeyRotationTests(SimpleTestCase):
    OLD = dict(IDENTITY_EMBEDDING_KEY="the-retired-secret-value-0123456789", IDENTITY_EMBEDDING_KEY_ID=1,
               IDENTITY_EMBEDDING_PREVIOUS_KEYS={})
    NEW = dict(IDENTITY_EMBEDDING_KEY="the-rotated-secret-value-0123456789", IDENTITY_EMBEDDING_KEY_ID=2)

    def test_records_written_before_rotation_stay_readable_until_re_encrypted(self):
        with override_settings(**self.OLD):
            old_record = protect_embedding(VECTOR)
        with override_settings(**self.NEW, IDENTITY_EMBEDDING_PREVIOUS_KEYS={1: self.OLD["IDENTITY_EMBEDDING_KEY"]}):
            np.testing.assert_array_equal(reveal_embedding(old_record, 3), VECTOR)
            self.assertEqual(payload_format(old_record), ("v2", 1))
            self.assertFalse(is_current_payload(old_record))
            fresh = protect_embedding(VECTOR)
            self.assertEqual(payload_format(fresh), ("v2", 2))
            self.assertTrue(is_current_payload(fresh))

    def test_retiring_the_old_key_makes_old_records_unreadable_rather_than_wrong(self):
        with override_settings(**self.OLD):
            old_record = protect_embedding(VECTOR)
        with override_settings(**self.NEW, IDENTITY_EMBEDDING_PREVIOUS_KEYS={}):
            with self.assertRaises(ValidationError):
                reveal_embedding(old_record, 3)

    def test_the_key_id_is_bound_to_the_key_so_it_cannot_be_relabelled(self):
        secret = "one-secret-value-0123456789-abcdef"
        with override_settings(IDENTITY_EMBEDDING_KEY=secret, IDENTITY_EMBEDDING_KEY_ID=1,
                               IDENTITY_EMBEDDING_PREVIOUS_KEYS={}):
            record = bytearray(protect_embedding(VECTOR))
        record[5] = 2  # claim it was written under key id 2, which holds the same secret
        with override_settings(IDENTITY_EMBEDDING_KEY=secret, IDENTITY_EMBEDDING_KEY_ID=1,
                               IDENTITY_EMBEDDING_PREVIOUS_KEYS={2: secret}):
            with self.assertRaises(ValidationError):
                reveal_embedding(bytes(record), 3)


class PreviousKeySettingTests(SimpleTestCase):
    def _load(self, value):
        code = (
            "import os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
            "from django.conf import settings; print(sorted(settings.IDENTITY_EMBEDDING_PREVIOUS_KEYS.items()))"
        )
        return subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR,
            env={**os.environ, "IDENTITY_EMBEDDING_PREVIOUS_KEYS": value},
            capture_output=True, text=True, timeout=60,
        )

    def test_previous_keys_are_parsed_from_id_secret_pairs(self):
        completed = self._load("1:first-secret, 7:has:colons:inside ,")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip().splitlines()[-1],
            "[(1, 'first-secret'), (7, 'has:colons:inside')]",
        )

    def test_a_malformed_list_stops_startup_instead_of_being_ignored(self):
        for bad in ("first-secret", "x:secret", "1:", ":secret"):
            with self.subTest(value=bad):
                completed = self._load(bad)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("IDENTITY_EMBEDDING_PREVIOUS_KEYS", completed.stderr)

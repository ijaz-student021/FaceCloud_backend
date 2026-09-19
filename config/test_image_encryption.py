import hashlib
import io
import os
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.http import FileResponse
from django.test import SimpleTestCase, override_settings
from PIL import Image

from config import image_crypto as crypto
from config.image_crypto import (
    CHUNK_SIZE,
    HEADER_LENGTH,
    SEALED_CHUNK,
    TAG_LENGTH,
    ImageEncryptionError,
    KeyRing,
)
from config.storage import EncryptedFileSystemStorage

SECRET = "unit-test-secret-one-0123456789abcdef"
OTHER_SECRET = "unit-test-secret-two-0123456789abcdef"
RING = KeyRing(1, {1: SECRET})


def pattern(length, seed=0):
    """Deterministic, non-repeating-looking bytes."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:length])


def seal(data, ring=RING, piece=None):
    pieces = [data] if piece is None else [data[i:i + piece] for i in range(0, len(data), piece)] or [b""]
    return b"".join(crypto.encrypt_chunks(pieces, ring))


def unseal(blob, ring=RING):
    reader = crypto.open_decrypted(io.BytesIO(blob), ring)
    try:
        return reader.read()
    finally:
        reader.close()


BOUNDARY_SIZES = (
    0, 1, 15, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE,
    2 * CHUNK_SIZE + 5, 3 * CHUNK_SIZE + 7,
)


class RoundTripTests(SimpleTestCase):
    def test_every_boundary_size_round_trips_with_the_expected_overhead(self):
        for size in BOUNDARY_SIZES:
            data = pattern(size)
            blob = seal(data)
            chunks = max(1, -(-size // CHUNK_SIZE))
            with self.subTest(size=size):
                self.assertEqual(unseal(blob), data)
                self.assertEqual(len(blob), HEADER_LENGTH + size + chunks * TAG_LENGTH)
                self.assertEqual(crypto.plaintext_size(len(blob)), size)

    def test_input_delivered_in_awkward_pieces_gives_the_same_plaintext(self):
        data = pattern(3 * CHUNK_SIZE + 123)
        for piece in (1, 7, 1000, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 10 * CHUNK_SIZE):
            with self.subTest(piece=piece):
                self.assertEqual(unseal(seal(data, piece=piece)), data)

    def test_plaintext_is_not_visible_and_each_encryption_is_different(self):
        data = b"SECRET-PIXELS-" * 5000
        first, second = seal(data), seal(data)

        self.assertNotIn(b"SECRET-PIXELS-", first)
        self.assertNotEqual(first, second)
        self.assertTrue(crypto.is_encrypted(first))
        self.assertEqual(crypto.key_id_of(first), 1)

    def test_random_access_reads_match_slices_across_chunk_boundaries(self):
        data = pattern(3 * CHUNK_SIZE + 500)
        reader = crypto.open_decrypted(io.BytesIO(seal(data)), RING)
        cases = [
            (0, 10), (CHUNK_SIZE - 5, 10), (CHUNK_SIZE, 1), (CHUNK_SIZE + 1, CHUNK_SIZE),
            (2 * CHUNK_SIZE - 1, 2), (len(data) - 20, 20), (len(data) - 20, 500),
        ]
        for offset, length in cases:
            with self.subTest(offset=offset, length=length):
                reader.seek(offset)
                self.assertEqual(reader.read(length), data[offset:offset + length])
        reader.seek(0, os.SEEK_END)
        self.assertEqual(reader.tell(), len(data))
        self.assertEqual(reader.read(10), b"")
        reader.seek(-100, os.SEEK_END)
        self.assertEqual(reader.read(), data[-100:])
        reader.seek(50)
        reader.seek(25, os.SEEK_CUR)
        self.assertEqual(reader.read(5), data[75:80])

    def test_a_pil_image_opens_directly_from_the_encrypted_bytes(self):
        buffer = io.BytesIO()
        Image.new("RGB", (300, 200), (10, 120, 200)).save(buffer, format="PNG")
        reader = crypto.open_decrypted(io.BytesIO(seal(buffer.getvalue())), RING)

        with Image.open(reader) as image:
            image.load()
            self.assertEqual(image.size, (300, 200))
            self.assertEqual(image.getpixel((5, 5)), (10, 120, 200))


class TamperingTests(SimpleTestCase):
    def setUp(self):
        self.data = pattern(3 * CHUNK_SIZE + 100)
        self.blob = seal(self.data)

    def read_all(self, blob, ring=RING):
        with self.assertRaises(ImageEncryptionError):
            unseal(blob, ring)

    def test_flipping_any_ciphertext_byte_is_detected(self):
        for position in (HEADER_LENGTH, HEADER_LENGTH + 5000, len(self.blob) - 1, len(self.blob) // 2):
            tampered = bytearray(self.blob)
            tampered[position] ^= 0x01
            with self.subTest(position=position):
                self.read_all(bytes(tampered))

    def test_changing_the_header_is_detected(self):
        for position in (len(crypto.MAGIC) + 1, len(crypto.MAGIC) + 4):  # nonce prefix bytes
            tampered = bytearray(self.blob)
            tampered[position] ^= 0x01
            with self.subTest(position=position):
                self.read_all(bytes(tampered))

    def test_cutting_chunks_off_the_end_is_detected(self):
        """The new last chunk was sealed as 'not final', so authentication fails."""
        for cut in (SEALED_CHUNK, 2 * SEALED_CHUNK):
            with self.subTest(cut=cut):
                self.read_all(self.blob[:-cut] if cut < len(self.blob) else self.blob[:HEADER_LENGTH + SEALED_CHUNK])

    def test_a_truncated_final_chunk_is_detected(self):
        self.read_all(self.blob[:-10])

    def test_appending_data_is_detected(self):
        self.read_all(self.blob + b"extra")

    def test_swapping_two_chunks_is_detected(self):
        chunks = [
            self.blob[HEADER_LENGTH + i * SEALED_CHUNK: HEADER_LENGTH + (i + 1) * SEALED_CHUNK]
            for i in range(2)
        ]
        swapped = self.blob[:HEADER_LENGTH] + chunks[1] + chunks[0] + self.blob[HEADER_LENGTH + 2 * SEALED_CHUNK:]
        self.read_all(swapped)

    def test_the_wrong_key_or_a_missing_key_id_is_refused(self):
        self.read_all(self.blob, KeyRing(1, {1: OTHER_SECRET}))
        with self.assertRaises(ImageEncryptionError):
            unseal(self.blob, KeyRing(2, {2: SECRET}))

    def test_a_non_encrypted_or_tiny_file_is_refused(self):
        for blob in (b"\x89PNG plain image bytes", b"", crypto.MAGIC):
            with self.subTest(blob=blob[:8]), self.assertRaises(ImageEncryptionError):
                crypto.open_decrypted(io.BytesIO(blob), RING)

    def test_an_empty_file_is_still_authenticated(self):
        blob = seal(b"")
        self.assertEqual(unseal(blob), b"")
        tampered = bytearray(blob)
        tampered[-1] ^= 1
        self.read_all(bytes(tampered))

    def test_truncating_a_file_down_to_nothing_is_refused_not_read_as_empty(self):
        for keep in (HEADER_LENGTH + TAG_LENGTH, HEADER_LENGTH + TAG_LENGTH + 3):
            with self.subTest(keep=keep), self.assertRaises(ImageEncryptionError):
                crypto.open_decrypted(io.BytesIO(self.blob[:keep]), RING)

    def test_errors_are_oserrors_so_unreadable_image_handling_applies(self):
        self.assertTrue(issubclass(ImageEncryptionError, OSError))


class KeyRotationTests(SimpleTestCase):
    def test_files_written_under_an_old_key_stay_readable_after_rotation(self):
        data = pattern(CHUNK_SIZE + 9)
        old = seal(data, KeyRing(1, {1: SECRET}))
        rotated = KeyRing(2, {2: OTHER_SECRET, 1: SECRET})

        self.assertEqual(unseal(old, rotated), data)
        new = seal(data, rotated)
        self.assertEqual(crypto.key_id_of(new), 2)
        self.assertEqual(unseal(new, rotated), data)
        with self.assertRaises(ImageEncryptionError):
            unseal(new, KeyRing(1, {1: SECRET}))  # the old ring cannot read new files

    def test_key_ids_are_validated(self):
        with self.assertRaises(ValueError):
            KeyRing(1, {2: SECRET})
        with self.assertRaises(ValueError):
            KeyRing(0, {0: SECRET})


@override_settings(
    IMAGE_ENCRYPTION_ENABLED=True, IMAGE_ENCRYPTION_KEY=SECRET,
    IMAGE_ENCRYPTION_KEY_ID=1, IMAGE_ENCRYPTION_PREVIOUS_KEYS={},
)
class EncryptedStorageTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = EncryptedFileSystemStorage(location=folder.name)
        self.folder = Path(folder.name)

    def png(self, size=(64, 48)):
        buffer = io.BytesIO()
        Image.new("RGB", size, (200, 30, 90)).save(buffer, format="PNG")
        return buffer.getvalue()

    def on_disk(self, name):
        return (self.folder / name).read_bytes()

    def test_saved_files_are_ciphertext_on_disk_and_plaintext_when_read(self):
        payload = self.png()
        name = self.storage.save("users/1/enhanced/a.png", ContentFile(payload))

        disk = self.on_disk(name)
        self.assertTrue(disk.startswith(crypto.MAGIC))
        self.assertNotIn(b"\x89PNG", disk)
        with self.storage.open(name) as handle:
            self.assertEqual(handle.read(), payload)

    def test_size_reports_the_plaintext_size(self):
        payload = self.png((400, 300))
        name = self.storage.save("users/1/enhanced/b.png", ContentFile(payload))

        self.assertEqual(self.storage.size(name), len(payload))
        self.assertGreater(self.storage.raw_size(name), len(payload))

    def test_large_multi_chunk_files_round_trip(self):
        payload = pattern(5 * CHUNK_SIZE + 321)
        name = self.storage.save("users/1/originals/big.bin", ContentFile(payload))

        with self.storage.open(name) as handle:
            self.assertEqual(handle.read(), payload)
        self.assertEqual(self.storage.size(name), len(payload))

    def test_the_opened_file_supports_chunks_and_images(self):
        payload = self.png((500, 400))
        name = self.storage.save("users/1/originals/c.png", ContentFile(payload))

        with self.storage.open(name) as handle:
            self.assertEqual(b"".join(handle.chunks(4096)), payload)
            handle.seek(0)
            with Image.open(handle) as image:
                self.assertEqual(image.size, (500, 400))

    def test_a_closed_file_can_be_reopened_like_an_ordinary_one(self):
        """Django's own File.open would look for the relative name on disk and fail."""
        payload = self.png()
        name = self.storage.save("users/1/originals/reopen.png", ContentFile(payload))

        handle = self.storage.open(name)
        self.assertEqual(handle.read(), payload)
        handle.close()
        self.assertTrue(handle.closed)

        handle.open("rb")
        self.assertEqual(handle.read(), payload)
        handle.open("rb")  # already open: rewinds
        self.assertEqual(handle.read(10), payload[:10])
        handle.close()

    def test_a_file_response_reports_the_correct_length(self):
        payload = pattern(3 * CHUNK_SIZE + 17)
        name = self.storage.save("users/1/originals/d.bin", ContentFile(payload))

        response = FileResponse(self.storage.open(name), as_attachment=True, filename="d.bin")

        self.assertEqual(int(response["Content-Length"]), len(payload))
        self.assertEqual(b"".join(response.streaming_content), payload)

    def test_plaintext_files_from_before_encryption_still_read_and_size_correctly(self):
        target = self.folder / "users/1/originals"
        target.mkdir(parents=True)
        legacy = self.png()
        (target / "old.png").write_bytes(legacy)

        with self.storage.open("users/1/originals/old.png") as handle:
            self.assertEqual(handle.read(), legacy)
        self.assertEqual(self.storage.size("users/1/originals/old.png"), len(legacy))
        self.assertFalse(self.storage.is_encrypted("users/1/originals/old.png"))
        self.assertIsNone(self.storage.key_id("users/1/originals/old.png"))

    def test_encrypted_files_stay_readable_when_the_flag_is_switched_off(self):
        payload = self.png()
        name = self.storage.save("users/1/enhanced/e.png", ContentFile(payload))

        with override_settings(IMAGE_ENCRYPTION_ENABLED=False):
            with self.storage.open(name) as handle:
                self.assertEqual(handle.read(), payload)
            second = self.storage.save("users/1/enhanced/plain.png", ContentFile(payload))
            self.assertEqual(self.on_disk(second), payload)  # new files are plaintext now

    def test_a_tampered_stored_file_raises_an_oserror_on_read(self):
        name = self.storage.save("users/1/enhanced/f.png", ContentFile(self.png()))
        path = self.folder / name
        data = bytearray(path.read_bytes())
        data[HEADER_LENGTH + 3] ^= 0xFF
        path.write_bytes(bytes(data))

        with self.assertRaises(OSError):
            with self.storage.open(name) as handle:
                handle.read()

    def test_the_key_id_of_stored_files_is_reported(self):
        name = self.storage.save("users/1/enhanced/g.png", ContentFile(self.png()))
        self.assertEqual(self.storage.key_id(name), 1)

    def test_delete_exists_and_listing_are_unaffected(self):
        name = self.storage.save("users/1/enhanced/h.png", ContentFile(self.png()))

        self.assertTrue(self.storage.exists(name))
        self.assertIn("h.png", self.storage.listdir("users/1/enhanced")[1])
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))

    def test_a_file_whose_key_id_is_unknown_cannot_be_read(self):
        name = self.storage.save("users/1/enhanced/i.png", ContentFile(self.png()))
        with override_settings(
            IMAGE_ENCRYPTION_KEY=OTHER_SECRET, IMAGE_ENCRYPTION_KEY_ID=2, IMAGE_ENCRYPTION_PREVIOUS_KEYS={},
        ):
            with self.assertRaises(ImageEncryptionError):
                self.storage.open(name)
        with override_settings(
            IMAGE_ENCRYPTION_KEY=OTHER_SECRET, IMAGE_ENCRYPTION_KEY_ID=2,
            IMAGE_ENCRYPTION_PREVIOUS_KEYS={1: SECRET},
        ):
            with self.storage.open(name) as handle:
                self.assertTrue(handle.read().startswith(b"\x89PNG"))

"""Encryption of stored images at rest (AES-256-GCM, chunked, seekable).

File layout::

    MAGIC "FCIMG1" | key id (1 byte) | nonce prefix (8 random bytes)
    chunk 0 | chunk 1 | ... | final chunk

Each chunk is the AES-GCM encryption of up to CHUNK_SIZE plaintext bytes followed
by its 16-byte tag. Properties:

* **Random access.** Chunks are a fixed size, so a reader can seek to any byte and
  decrypt only the chunks it needs. Images can be opened by PIL/OpenCV, served
  with a correct Content-Length, or streamed without holding the file in memory or
  writing plaintext to a temporary file.
* **Tamper and truncation detection.** Every chunk's authenticated data is the
  header, the chunk's index, and whether it is the last chunk. Reordering,
  swapping, editing or cutting chunks off the end (which would make an earlier
  chunk look like the last) fails authentication.
* **Unique nonces.** 8 random bytes per file plus a 32-bit chunk counter.
* **Key rotation.** The key id in the header selects the key; the key for an id
  is derived with HKDF-SHA256 from the configured secret.
* **Empty files** are one empty final chunk, so they are authenticated too.

The plaintext size is derived from the ciphertext size, so nothing extra is
stored and `size()` needs no decryption.
"""
import functools
import io
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"FCIMG1"
CHUNK_SIZE = 64 * 1024
TAG_LENGTH = 16
NONCE_PREFIX_LENGTH = 8
HEADER_LENGTH = len(MAGIC) + 1 + NONCE_PREFIX_LENGTH
SEALED_CHUNK = CHUNK_SIZE + TAG_LENGTH


class ImageEncryptionError(OSError):
    """The stored file is corrupt, was tampered with, or its key is unavailable.

    An OSError so existing "the file cannot be read" handling treats it as an
    unreadable image instead of crashing.
    """


class KeyRing:
    """The active key plus retired keys that can still read older files."""

    def __init__(self, primary_id, secrets):
        if primary_id not in secrets:
            raise ValueError("The primary key id has no secret.")
        for key_id in secrets:
            if not 1 <= key_id <= 255:
                raise ValueError("Key ids must be between 1 and 255.")
        self.primary_id = primary_id
        self._secrets = dict(secrets)

    def aead(self, key_id):
        secret = self._secrets.get(key_id)
        if secret is None:
            raise ImageEncryptionError(f"No key is configured for stored-image key id {key_id}.")
        return _aead(secret, key_id)


@functools.lru_cache(maxsize=32)
def _aead(secret, key_id):
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"facecloud/stored-image/v1/key-" + str(key_id).encode("ascii"),
    ).derive(secret.encode("utf-8"))
    return AESGCM(key)


def _nonce(prefix, index):
    return prefix + index.to_bytes(4, "big")


def _associated_data(header, index, final):
    return header + index.to_bytes(8, "big") + (b"\x01" if final else b"\x00")


def is_encrypted(head):
    """True when ``head`` (the first bytes of a file) starts an encrypted image."""
    return head[: len(MAGIC)] == MAGIC


def key_id_of(head):
    return head[len(MAGIC)] if len(head) > len(MAGIC) else None


def plaintext_size(ciphertext_size):
    """Size of the original file, computed from the size of the encrypted one."""
    body = ciphertext_size - HEADER_LENGTH
    if body < TAG_LENGTH:
        raise ImageEncryptionError("The stored image is truncated.")
    chunks = -(-body // SEALED_CHUNK)
    return body - chunks * TAG_LENGTH


def encrypt_chunks(pieces, keyring):
    """Yield the encrypted form of ``pieces`` (an iterable of plaintext ``bytes``)."""
    prefix = os.urandom(NONCE_PREFIX_LENGTH)
    header = MAGIC + bytes([keyring.primary_id]) + prefix
    aead = keyring.aead(keyring.primary_id)
    yield header

    buffer = bytearray()
    index = 0
    pending = None  # the last full chunk, held back until we know it is not the final one
    for piece in pieces:
        buffer.extend(piece)
        while len(buffer) >= CHUNK_SIZE:
            if pending is not None:
                yield aead.encrypt(_nonce(prefix, index), pending, _associated_data(header, index, False))
                index += 1
            pending = bytes(buffer[:CHUNK_SIZE])
            del buffer[:CHUNK_SIZE]
    if pending is not None and buffer:
        yield aead.encrypt(_nonce(prefix, index), pending, _associated_data(header, index, False))
        index += 1
        pending = None
    final = pending if pending is not None else bytes(buffer)
    yield aead.encrypt(_nonce(prefix, index), final, _associated_data(header, index, True))


class EncryptedReader(io.RawIOBase):
    """A seekable, read-only view of the plaintext of an encrypted file."""

    def __init__(self, raw, keyring):
        super().__init__()
        self._raw = raw
        self._keyring = keyring
        raw.seek(0, os.SEEK_END)
        self._ciphertext_size = raw.tell()
        raw.seek(0)
        header = raw.read(HEADER_LENGTH)
        if len(header) != HEADER_LENGTH or not is_encrypted(header):
            raise ImageEncryptionError("The stored image is not in the encrypted format.")
        self._header = header
        self._prefix = header[len(MAGIC) + 1:]
        self._aead = keyring.aead(header[len(MAGIC)])
        self._size = plaintext_size(self._ciphertext_size)
        self._chunks = -(-(self._ciphertext_size - HEADER_LENGTH) // SEALED_CHUNK)
        self._position = 0
        self._cached_index = None
        self._cached_plain = b""
        # Authenticate the final chunk now. It vouches for the file's length, so
        # truncation (even down to an empty file, which would otherwise never be
        # decrypted at all) is refused at open rather than partway through a read.
        self._chunk(self._chunks - 1)

    # -- io.RawIOBase -------------------------------------------------------
    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._position + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError("Invalid whence.")
        if target < 0:
            raise ValueError("Negative seek position.")
        self._position = target
        return target

    def readinto(self, buffer):
        view = memoryview(buffer).cast("B")
        wanted = len(view)
        written = 0
        while written < wanted and self._position < self._size:
            index = self._position // CHUNK_SIZE
            plain = self._chunk(index)
            start = self._position - index * CHUNK_SIZE
            take = min(wanted - written, len(plain) - start)
            view[written:written + take] = plain[start:start + take]
            written += take
            self._position += take
        return written

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()

    # -- decryption ---------------------------------------------------------
    def _chunk(self, index):
        if index == self._cached_index:
            return self._cached_plain
        self._raw.seek(HEADER_LENGTH + index * SEALED_CHUNK)
        sealed = self._raw.read(SEALED_CHUNK)
        final = index == self._chunks - 1
        try:
            plain = self._aead.decrypt(
                _nonce(self._prefix, index), sealed, _associated_data(self._header, index, final),
            )
        except InvalidTag:
            raise ImageEncryptionError("The stored image failed authentication.") from None
        self._cached_index, self._cached_plain = index, plain
        return plain


def open_decrypted(raw, keyring):
    """A buffered, seekable binary file object over the plaintext."""
    return io.BufferedReader(EncryptedReader(raw, keyring), buffer_size=CHUNK_SIZE)

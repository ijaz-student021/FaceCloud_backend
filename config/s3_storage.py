"""Private S3 storage with client-side encryption (Feature 43).

The same promise as ``config.storage.EncryptedFileSystemStorage``, on an object store:

* Every FileField uses Django's default storage, so selecting this backend (``STORAGE_BACKEND=s3``) covers
  originals, standardized and enhanced copies, edits, previews, comparison outputs, reports and export packages
  without touching the code that reads or writes them.
* With ``IMAGE_ENCRYPTION_ENABLED`` a file is encrypted (AES-256-GCM, key ring in ``config.image_crypto``) **before it
  leaves the server**, so the bucket only ever holds ciphertext, and S3's own server-side encryption sits on top
  of it. Reading never depends on the flag: an object that starts with the encrypted-image header is decrypted,
  anything else is returned as is.
* The objects are private. Downloads are streamed through the authenticated API (which also decrypts), so no signed
  URL for ciphertext is ever handed out: ``url()`` refuses while encryption is on.
* Object keys are the secure UUID names the upload code already generates (``users/<id>/<kind>/<uuid>.<ext>``); the
  original file name is metadata in the database, never part of a key.
* ``size()`` reports the plaintext size, which is what callers mean by it.

Credentials come from the standard boto3 chain (an IAM role, or an IAM user's keys in the environment): never the
account's root keys; ``manage.py check_storage`` refuses them.
"""
import tempfile

from django.conf import settings
from django.core.files import File
from storages.backends.s3 import S3Storage
from storages.utils import clean_name

from . import image_crypto
from .storage import _DecryptedFile, keyring

_SPOOL_BYTES = 16 * 1024 * 1024  # ciphertext is held in memory up to this size, then spills to a temporary file


class EncryptedS3Storage(S3Storage):
    def _save(self, name, content):
        if settings.IMAGE_ENCRYPTION_ENABLED:
            spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115 - closed by the upload below
            try:
                for piece in image_crypto.encrypt_chunks(content.chunks(image_crypto.CHUNK_SIZE), keyring()):
                    spool.write(piece)
                spool.seek(0)
                return super()._save(name, File(spool, name=name))
            finally:
                spool.close()
        return super()._save(name, content)

    def _read_head(self, name):
        """The first bytes of the stored object, by a ranged request (the whole object is not downloaded)."""
        key = self._normalize_name(clean_name(name))
        response = self.bucket.Object(key).get(Range=f"bytes=0-{image_crypto.HEADER_LENGTH - 1}")
        return response["Body"].read()

    def is_encrypted(self, name):
        return image_crypto.is_encrypted(self._read_head(name))

    def key_id(self, name):
        head = self._read_head(name)
        return image_crypto.key_id_of(head) if image_crypto.is_encrypted(head) else None

    def _decrypting_reader(self, name):
        raw = super()._open(name, "rb").file
        try:
            return image_crypto.open_decrypted(raw, keyring())
        except Exception:
            raw.close()
            raise

    def _open(self, name, mode="rb"):
        if "r" not in mode or any(flag in mode for flag in "wa+"):
            return super()._open(name, mode)
        if not self.is_encrypted(name):
            return super()._open(name, mode)
        return _DecryptedFile(self, name)

    def size(self, name):
        raw_size = super().size(name)
        if raw_size >= image_crypto.HEADER_LENGTH and self.is_encrypted(name):
            return image_crypto.plaintext_size(raw_size)
        return raw_size

    def raw_size(self, name):
        """Stored size, whatever the format."""
        return super().size(name)

    def url(self, name, parameters=None, expire=None, http_method=None):
        if settings.IMAGE_ENCRYPTION_ENABLED:
            raise NotImplementedError(
                "Stored images are client-side encrypted; serve them through the authenticated API, "
                "not through a signed URL."
            )
        return super().url(name, parameters=parameters, expire=expire, http_method=http_method)

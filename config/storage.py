"""File storage that encrypts stored images at rest.

Every FileField in the project uses Django's default storage, so switching the
default backend to this class covers originals, standardized copies, enhanced and
edited images, comparison previews, reports and export packages without touching
the code that reads or writes them.

* ``IMAGE_ENCRYPTION_ENABLED`` decides whether *new* files are encrypted.
* Reading never depends on the flag: a file that starts with the encrypted-image
  header is decrypted, anything else is returned as is. Files written before
  encryption was switched on therefore keep working until
  ``manage.py encrypt_stored_media`` converts them.
* ``size()`` reports the plaintext size, which is what callers mean by it.
"""
from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage

from . import image_crypto


def keyring():
    """The key ring described by the IMAGE_ENCRYPTION_* settings."""
    secrets = {settings.IMAGE_ENCRYPTION_KEY_ID: settings.IMAGE_ENCRYPTION_KEY}
    for key_id, secret in settings.IMAGE_ENCRYPTION_PREVIOUS_KEYS.items():
        secrets.setdefault(key_id, secret)
    secrets = {key_id: secret for key_id, secret in secrets.items() if secret}
    if settings.IMAGE_ENCRYPTION_KEY_ID not in secrets:
        raise image_crypto.ImageEncryptionError("IMAGE_ENCRYPTION_KEY is not configured.")
    return image_crypto.KeyRing(settings.IMAGE_ENCRYPTION_KEY_ID, secrets)


class _EncryptingContent(File):
    """Wraps content being saved so the storage writes ciphertext instead."""

    def __init__(self, content, ring):
        super().__init__(content, name=getattr(content, "name", None))
        self._content = content
        self._ring = ring

    def chunks(self, chunk_size=None):
        return image_crypto.encrypt_chunks(self._content.chunks(image_crypto.CHUNK_SIZE), self._ring)


class _DecryptedFile(File):
    """A plaintext view of an encrypted stored file that can be reopened.

    Django's ``File.open`` reopens a closed file by treating its name as a path on
    disk, which does not work for a name relative to the storage root. Keeping the
    storage here lets ``field.open()`` after ``field.close()`` behave as it does
    for ordinary files.
    """

    def __init__(self, storage, name):
        self._storage = storage
        super().__init__(storage._decrypting_reader(name), name=name)

    def open(self, mode=None):
        if self.closed:
            self.file = self._storage._decrypting_reader(self.name)
        else:
            self.seek(0)
        return self


class EncryptedFileSystemStorage(FileSystemStorage):
    def _save(self, name, content):
        if settings.IMAGE_ENCRYPTION_ENABLED:
            content = _EncryptingContent(content, keyring())
        return super()._save(name, content)

    def _read_head(self, name):
        with super()._open(name, "rb") as handle:
            return handle.read(image_crypto.HEADER_LENGTH)

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
        file = super()._open(name, mode)
        if "r" not in mode or any(flag in mode for flag in "wa+"):
            return file
        raw = file.file
        head = raw.read(image_crypto.HEADER_LENGTH)
        if not image_crypto.is_encrypted(head):
            raw.seek(0)
            return file
        raw.close()
        return _DecryptedFile(self, name)

    def size(self, name):
        raw_size = super().size(name)
        if raw_size >= image_crypto.HEADER_LENGTH and self.is_encrypted(name):
            return image_crypto.plaintext_size(raw_size)
        return raw_size

    def raw_size(self, name):
        """On-disk size, whatever the format."""
        return super().size(name)

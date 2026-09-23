"""Convert stored media to the encrypted format, or to the current key.

`config.storage.EncryptedFileSystemStorage` encrypts files as they are written,
but files that existed before encryption was switched on stay plaintext, and
files written under a retired key keep that key id. This walks the media folders
and rewrites them, safely:

* the new file is written next to the old one, then read back and compared with
  the original by SHA-256 before it replaces it (atomically);
* the modification time is kept, so the orphaned-media grace period still works;
* a file that cannot be converted is left untouched and counted as failed;
* it is idempotent and resumable, so a partial run can simply be run again.
"""
import hashlib
import logging
import os

from django.conf import settings
from django.core.files.storage import default_storage

from config import image_crypto
from config.storage import EncryptedFileSystemStorage, keyring
from privacy.cleanup import MEDIA_SUBDIRECTORIES

logger = logging.getLogger(__name__)
TEMPORARY_SUFFIX = ".encrypting"


def media_names(storage=None):
    """Every stored file under users/<id>/<known folder>/, referenced or not."""
    storage = storage or default_storage
    try:
        user_directories, _files = storage.listdir("users")
    except (FileNotFoundError, OSError):
        return
    for user_directory in sorted(user_directories):
        for subdirectory in MEDIA_SUBDIRECTORIES:
            directory = f"users/{user_directory}/{subdirectory}"
            try:
                _dirs, files = storage.listdir(directory)
            except (FileNotFoundError, OSError):
                continue
            for filename in sorted(files):
                if not filename.endswith(TEMPORARY_SUFFIX):
                    yield f"{directory}/{filename}"


def _digest_of(handle):
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _rewrite(storage, name, ring):
    """Rewrite one file under the primary key; returns its plaintext size."""
    path = storage.path(name)
    temporary = path + TEMPORARY_SUFFIX
    original = os.stat(path)
    try:
        with storage.open(name) as source:
            expected = _digest_of(source)
            source.seek(0)
            with open(temporary, "wb") as target:
                for piece in image_crypto.encrypt_chunks(iter(lambda: source.read(1024 * 1024), b""), ring):
                    target.write(piece)
                target.flush()
                os.fsync(target.fileno())
        with open(temporary, "rb") as written:
            reader = image_crypto.open_decrypted(written, ring)
            actual = _digest_of(reader)
        if actual != expected:
            raise image_crypto.ImageEncryptionError("The re-encrypted copy did not match the original.")
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return storage.size(name)


def encrypt_stored_media(*, dry_run=False, rotate=False, storage=None):
    """Encrypt plaintext media (and, with ``rotate``, media under an older key)."""
    storage = storage or default_storage
    if not isinstance(storage, EncryptedFileSystemStorage):
        raise RuntimeError("The default storage does not support encryption.")
    if not settings.IMAGE_ENCRYPTION_ENABLED:
        raise RuntimeError("Set IMAGE_ENCRYPTION_ENABLED=True and IMAGE_ENCRYPTION_KEY first.")
    ring = keyring()
    summary = {
        "dry_run": dry_run, "scanned": 0, "already_current": 0,
        "encrypted": 0, "rotated": 0, "failed": 0, "bytes": 0,
    }
    for name in media_names(storage):
        summary["scanned"] += 1
        try:
            key_id = storage.key_id(name)
            if key_id is None:
                outcome = "encrypted"
            elif rotate and key_id != ring.primary_id:
                outcome = "rotated"
            else:
                summary["already_current"] += 1
                continue
            if not dry_run:
                summary["bytes"] += _rewrite(storage, name, ring)
            summary[outcome] += 1
        except OSError:
            logger.exception("Could not convert stored file %s; it was left unchanged.", name)
            summary["failed"] += 1
    return summary

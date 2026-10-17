"""Copy existing local media into the private S3 bucket (``manage.py copy_media_to_s3``).

The local files are only read: nothing is deleted or modified, so the run can be repeated and the old copy removed by
hand once the bucket has been verified. Each file is stored under the same name (``users/<id>/<kind>/<uuid>.<ext>``) beneath
the configured key prefix:

* a file that is already encrypted is copied byte for byte (it stays readable with the same key ring);
* a plaintext file is encrypted on the way in when ``IMAGE_ENCRYPTION_ENABLED`` is on;
* every copy is read back through the storage and compared with the original's SHA-256 before it counts as copied;
* a file already in the bucket with the same plaintext digest is skipped, so an interrupted run resumes.
"""
import hashlib
import os
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from storages.utils import clean_name

from config import image_crypto
from config.s3_storage import EncryptedS3Storage
from config.storage import keyring


def local_files(root):
    root = Path(root)
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")):
        yield path, path.relative_to(root).as_posix()


def _plaintext_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        head = handle.read(image_crypto.HEADER_LENGTH)
        handle.seek(0)
        stream = image_crypto.open_decrypted(handle, keyring()) if image_crypto.is_encrypted(head) else handle
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stored_digest(storage, name):
    digest = hashlib.sha256()
    with storage.open(name, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_media(storage, root, dry_run=False, limit=None):
    """Returns ``{"copied", "skipped", "failed", "bytes", "failures"}``."""
    summary = {"copied": 0, "skipped": 0, "failed": 0, "bytes": 0, "failures": []}
    for index, (path, name) in enumerate(local_files(root)):
        if limit is not None and index >= limit:
            break
        try:
            expected = _plaintext_digest(path)
            if storage.exists(name) and _stored_digest(storage, name) == expected:
                summary["skipped"] += 1
                continue
            if dry_run:
                summary["copied"] += 1
                continue
            with open(path, "rb") as handle:
                head = handle.read(image_crypto.HEADER_LENGTH)
            if storage.exists(name):
                storage.delete(name)
            if image_crypto.is_encrypted(head):
                key = storage._normalize_name(clean_name(name))
                with open(path, "rb") as handle:
                    storage.bucket.Object(key).put(Body=handle, **storage.get_object_parameters(name))
            else:
                with open(path, "rb") as handle:
                    storage.save(name, ContentFile(handle.read()))
            if _stored_digest(storage, name) != expected:
                storage.delete(name)
                raise ValueError("The copy read back from the bucket does not match the original.")
            summary["copied"] += 1
            summary["bytes"] += os.path.getsize(path)
        except Exception as error:  # a file that cannot be copied is reported and left; the run goes on
            summary["failed"] += 1
            summary["failures"].append({"file": name, "error": f"{type(error).__name__}: {str(error)[:140]}"})
    return summary


def default_root():
    return settings.MEDIA_ROOT


def new_storage():
    return EncryptedS3Storage()

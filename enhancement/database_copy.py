"""A copy of each enhanced image inside the database, so it can be opened from a database viewer (ENHANCED_IMAGE_DATABASE_COPY_ENABLED).

The stored file stays the image the product serves. The copy is a row in ``enhancement_enhancedimageblob`` holding the same bytes with their size and
SHA-256. It is plain bytes, so it is never made while ``IMAGE_ENCRYPTION_ENABLED`` is on or from a file that is stored encrypted: it must not weaken the
encryption at rest. A failure here never touches the result.
"""
import hashlib
import logging
import mimetypes
from io import BytesIO

from django.conf import settings
from django.db import transaction
from PIL import Image, UnidentifiedImageError

from .models import EnhancedImageBlob

logger = logging.getLogger(__name__)


def content_type_of(name):
    guessed = mimetypes.guess_type(name)[0]
    return guessed if guessed and guessed.startswith("image/") else "application/octet-stream"


def stored_encrypted(field_file):
    check = getattr(field_file.storage, "is_encrypted", None)
    return bool(check and check(field_file.name))


def read_stored(field_file):
    with field_file.storage.open(field_file.name, "rb") as handle:
        return handle.read()


def store_copy(result, write=True):
    """Copy ``result``'s stored enhanced image into the database.

    Returns ``(outcome, copy)``: ``stored``, ``unchanged`` (already copied from this file), ``encrypted`` (not copied, see the module text),
    ``no_file``, ``file_missing`` or ``size_differs`` (the file is not the size the result recorded, so nothing is copied). With ``write=False`` nothing is read or
    written and a copy that would be made is reported as ``would_store``.
    """
    field = result.enhanced_file
    if not field:
        return "no_file", None
    try:
        encrypted = settings.IMAGE_ENCRYPTION_ENABLED or stored_encrypted(field)
    except OSError:
        return "file_missing", None
    if encrypted:
        return "encrypted", None
    existing = EnhancedImageBlob.objects.filter(result=result).only("source_name", "byte_size", "sha256").first()
    if existing and existing.source_name == field.name:
        return "unchanged", existing
    if not write:
        return "would_store", None
    try:
        data = read_stored(field)
    except OSError:
        return "file_missing", None
    if result.output_file_size and len(data) != result.output_file_size:
        return "size_differs", None
    values = {
        "content_type": content_type_of(field.name),
        "byte_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_name": field.name,
        "data": data,
    }
    with transaction.atomic():  # a savepoint: a failed write must not poison the caller's transaction
        copy, _created = EnhancedImageBlob.objects.update_or_create(result=result, defaults=values)
    return "stored", copy


def copy_after_completion(result):
    """Called when an enhancement has just been published. Never raises: the result is never put at risk by its copy."""
    if not settings.ENHANCED_IMAGE_DATABASE_COPY_ENABLED:
        return
    try:
        outcome, _copy = store_copy(result)
    except Exception:
        logger.exception("The database copy of the enhanced image of result %s failed; the stored file is unaffected.", result.pk)
        return
    if outcome == "encrypted":
        logger.info("No database copy of result %s: stored images are encrypted and the copy would be plain bytes.", result.pk)
    elif outcome == "size_differs":
        logger.warning("No database copy of result %s: the stored file is not the size the result recorded.", result.pk)


def verify_copy(result):
    """``"ok"``, or what is wrong with the database copy of ``result``'s image: missing, file_missing, bytes_differ, not_an_image or size_differs."""
    row = EnhancedImageBlob.objects.filter(result=result).first()
    if row is None:
        return "missing"
    data = bytes(row.data)  # PostgreSQL hands back a memoryview
    if len(data) != row.byte_size or hashlib.sha256(data).hexdigest() != row.sha256:
        return "bytes_differ"
    try:
        stored = read_stored(result.enhanced_file)
    except (FileNotFoundError, OSError, ValueError):
        return "file_missing"
    if stored != data:
        return "bytes_differ"
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            size = image.size
    except (UnidentifiedImageError, OSError):
        return "not_an_image"
    if result.output_width and result.output_height and size != (result.output_width, result.output_height):
        return "size_differs"
    return "ok"

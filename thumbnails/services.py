import io
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from PIL import Image

from uploads.decoding import to_working_colour

from .models import ImageThumbnail

logger = logging.getLogger(__name__)


class ThumbnailUnavailable(Exception):
    """The photo cannot be turned into a thumbnail (missing or undecodable)."""


def allowed_sizes():
    return tuple(int(size) for size in settings.THUMBNAIL_SIZES)


def default_size():
    return allowed_sizes()[0]


def thumbnail_url(image_id, request=None):
    path = f"/api/images/{image_id}/thumbnail/"
    return request.build_absolute_uri(path) if request else path


def render_thumbnail(image_field, size):
    """Return ``(jpeg_bytes, width, height)`` with the longest edge at most ``size``.

    Never enlarges a small photo, applies the EXIF orientation, and flattens
    transparency onto white (JPEG has no alpha).
    """
    try:
        image_field.open("rb")
        try:
            with Image.open(image_field) as source:
                if source.format == "JPEG":
                    # Let the decoder skip most of the work for a big JPEG.
                    source.draft("RGB", (size * 2, size * 2))
                source.load()
                working, _notes = to_working_colour(source)
                if working.mode == "RGBA":
                    rgba = working.convert("RGBA")
                    flattened = Image.new("RGB", rgba.size, (255, 255, 255))
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    working = flattened
                else:
                    working = working.convert("RGB")
                working.thumbnail((size, size), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                working.save(buffer, format="JPEG", quality=int(settings.THUMBNAIL_JPEG_QUALITY), optimize=True)
                return buffer.getvalue(), working.width, working.height
        finally:
            image_field.close()
    except ThumbnailUnavailable:
        raise
    except Exception as error:
        raise ThumbnailUnavailable("The image could not be read.") from error


def get_or_create_thumbnail(image, size):
    """The cached thumbnail for ``image`` at ``size``, generating it when missing."""
    existing = ImageThumbnail.objects.filter(image=image, size=size).first()
    if existing is not None:
        if existing.file and existing.file.storage.exists(existing.file.name):
            return existing
        existing.delete()  # the stored file vanished; make a fresh one

    if not image.original_file:
        raise ThumbnailUnavailable("The stored image is unavailable.")
    payload, width, height = render_thumbnail(image.original_file, size)
    thumbnail = ImageThumbnail(
        image=image, owner_id=image.owner_id, size=size, width=width, height=height, file_size=len(payload),
    )
    thumbnail.file.save("thumbnail.jpg", ContentFile(payload), save=False)
    try:
        with transaction.atomic():
            thumbnail.save()
    except IntegrityError:
        # A concurrent request generated it first: keep theirs, drop our file.
        thumbnail.file.storage.delete(thumbnail.file.name)
        winner = ImageThumbnail.objects.filter(image=image, size=size).first()
        if winner is None:
            raise
        return winner
    return thumbnail

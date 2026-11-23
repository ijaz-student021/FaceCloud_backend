"""iPhone photographs (HEIC / HEIF) are accepted by turning them into a JPEG on the way in.

Everything after the upload (preprocessing, the face check, the enhancement engine, thumbnails, downloads, the OpenCV readers) reads a JPEG, so a HEIC is decoded ONCE here
(``pillow-heif``, which carries libheif) and stored as a high-quality JPEG (quality ``HEIC_JPEG_QUALITY``, the orientation applied, the capture time kept). The stored original is
that JPEG: its name keeps the photograph's own name with a ``.jpg`` ending, and a download gives the JPEG back. When ``pillow-heif`` is not installed a HEIC is refused with a plain
sentence, like any other unsupported file.
"""
import io
import logging
from pathlib import PurePosixPath

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)

HEIF_EXTENSIONS = {".heic", ".heif"}
HEIF_FORMATS = {"HEIF", "HEIC"}
_registered = None


def available():
    """Whether ``pillow-heif`` can be used (installed, and its opener registered with Pillow)."""
    global _registered
    if _registered is None:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _registered = True
        except Exception:      # not installed, or its native library cannot load
            logger.warning("HEIC/HEIF uploads are off: pillow-heif is not available.", exc_info=True)
            _registered = False
    return _registered


def is_heif_name(name):
    return PurePosixPath(str(name or "")).suffix.lower() in HEIF_EXTENSIONS


def to_jpeg(uploaded_file, name):
    """``(SimpleUploadedFile, new_name)``: the JPEG made from a HEIC/HEIF ``uploaded_file`` named ``name``. Raises ``ValidationError`` for anything that is not a readable HEIF."""
    if not available():
        raise ValidationError("HEIC/HEIF photographs are not supported on this server. Export the photo as JPG and upload that.")
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as source:
            if source.format not in HEIF_FORMATS:
                raise ValidationError("The filename extension does not match the image content.")
            width, height = source.size
            if width * height > settings.MAX_IMAGE_PIXELS:
                raise ValidationError("The image dimensions exceed the configured pixel limit.")
            source.load()
            exif = source.getexif()
            oriented = ImageOps.exif_transpose(source)
            picture = oriented.convert("RGB")
            exif.pop(0x0112, None)             # the orientation is applied to the pixels: the stored file must not rotate them again
            buffer = io.BytesIO()
            picture.save(buffer, "JPEG", quality=int(settings.HEIC_JPEG_QUALITY), subsampling=0, optimize=True, exif=exif.tobytes() if len(exif) else b"")
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValidationError("The HEIC/HEIF file is not a valid or readable image.") from None
    finally:
        uploaded_file.seek(0)
    new_name = str(PurePosixPath(str(name)).with_suffix(".jpg"))
    return SimpleUploadedFile(new_name, buffer.getvalue(), content_type="image/jpeg"), new_name

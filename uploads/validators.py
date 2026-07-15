import hashlib
import stat
import zipfile
from pathlib import PurePosixPath

from django.conf import settings
from PIL import Image, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
FORMAT_DETAILS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
}


def format_size(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}"
        size /= 1024


def safe_original_name(name):
    normalized = str(name or "").replace("\\", "/")
    result = PurePosixPath(normalized).name.strip()
    return result[:255] or "unnamed"


def validate_image_file(uploaded_file, original_name=None):
    name = safe_original_name(original_name or uploaded_file.name)
    extension = PurePosixPath(name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValidationError("Unsupported file type. Only JPG, JPEG, and PNG are allowed.")
    if uploaded_file.size <= 0:
        raise ValidationError("The image file is empty.")
    if uploaded_file.size > settings.MAX_IMAGE_UPLOAD_SIZE:
        raise ValidationError(
            f"The image exceeds the {format_size(settings.MAX_IMAGE_UPLOAD_SIZE)} upload limit."
        )

    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
        if image_format not in FORMAT_DETAILS:
            raise ValidationError("Unsupported image content.")
        canonical_extension, mime_type = FORMAT_DETAILS[image_format]
        if image_format == "JPEG" and extension not in {".jpg", ".jpeg"}:
            raise ValidationError("The filename extension does not match the image content.")
        if image_format == "PNG" and extension != ".png":
            raise ValidationError("The filename extension does not match the image content.")
        if width * height > settings.MAX_IMAGE_PIXELS:
            raise ValidationError("The image dimensions exceed the configured pixel limit.")
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise ValidationError("The file is not a valid or readable image.")
    finally:
        uploaded_file.seek(0)

    digest = hashlib.sha256()
    for chunk in iter(lambda: uploaded_file.read(1024 * 1024), b""):
        digest.update(chunk)
    uploaded_file.seek(0)
    return {
        "original_name": name,
        "file_extension": canonical_extension,
        "mime_type": mime_type,
        "file_size": uploaded_file.size,
        "width": width,
        "height": height,
        "checksum_sha256": digest.hexdigest(),
    }


def validate_zip_archive(uploaded_file):
    name = safe_original_name(uploaded_file.name)
    if not name.lower().endswith(".zip"):
        raise ValidationError("The uploaded archive must be a ZIP file.")
    if uploaded_file.size <= 0:
        raise ValidationError("The ZIP archive is empty.")
    if uploaded_file.size > settings.MAX_ZIP_UPLOAD_SIZE:
        raise ValidationError(
            f"The ZIP archive exceeds the {format_size(settings.MAX_ZIP_UPLOAD_SIZE)} upload limit."
        )
    uploaded_file.seek(0)
    if not zipfile.is_zipfile(uploaded_file):
        uploaded_file.seek(0)
        raise ValidationError("The uploaded file is not a valid ZIP archive.")
    uploaded_file.seek(0)
    try:
        with zipfile.ZipFile(uploaded_file) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if not members:
                raise ValidationError("The ZIP archive contains no files.")
            if len(members) > settings.MAX_UPLOAD_BATCH_FILES:
                raise ValidationError(f"A ZIP batch cannot contain more than {settings.MAX_UPLOAD_BATCH_FILES} files.")
            total_size = 0
            for member in members:
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise ValidationError("The ZIP archive contains an unsafe path.")
                if member.flag_bits & 0x1:
                    raise ValidationError("Password-protected ZIP entries are not supported.")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValidationError("Symbolic links are not allowed in ZIP archives.")
                if path.suffix.lower() == ".zip":
                    raise ValidationError("Nested ZIP archives are not supported.")
                if member.file_size > settings.MAX_IMAGE_UPLOAD_SIZE:
                    raise ValidationError(
                        f"{safe_original_name(member.filename)} exceeds the "
                        f"{format_size(settings.MAX_IMAGE_UPLOAD_SIZE)} image limit."
                    )
                ratio = member.file_size / max(member.compress_size, 1)
                if ratio > settings.MAX_ZIP_COMPRESSION_RATIO:
                    raise ValidationError("The ZIP archive contains a suspicious compression ratio.")
                total_size += member.file_size
                if total_size > settings.MAX_ZIP_UNCOMPRESSED_SIZE:
                    raise ValidationError("The ZIP archive exceeds the allowed uncompressed size.")
            return members
    except zipfile.BadZipFile:
        raise ValidationError("The ZIP archive is corrupted.")
    finally:
        uploaded_file.seek(0)

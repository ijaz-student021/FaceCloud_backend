import tempfile
import zipfile

from django.core.files import File
from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import ImageUpload, UploadBatch
from .validators import safe_original_name, validate_image_file, validate_zip_archive


def _error_text(error):
    detail = getattr(error, "detail", error)
    if isinstance(detail, (list, tuple)):
        return " ".join(str(item) for item in detail)
    return str(detail)


def _create_rejected(batch, owner, filename, reason, size=0):
    return ImageUpload.objects.create(
        owner=owner,
        batch=batch,
        original_name=safe_original_name(filename),
        file_size=max(size, 0),
        status="rejected",
        rejection_reason=reason,
    )


def _save_valid_image(batch, owner, uploaded_file, original_name=None):
    metadata = validate_image_file(uploaded_file, original_name)
    image = ImageUpload(owner=owner, batch=batch, status="uploaded", **metadata)
    try:
        image.original_file.save(metadata["original_name"], uploaded_file, save=False)
        image.save()
    except Exception:
        if image.original_file:
            image.original_file.storage.delete(image.original_file.name)
        raise
    return image


def _finalize_batch(batch):
    batch.total_files = batch.images.count()
    batch.accepted_files = batch.images.filter(status="uploaded").count()
    batch.rejected_files = batch.images.filter(status="rejected").count()
    batch.total_size = sum(batch.images.values_list("file_size", flat=True))
    if batch.accepted_files and batch.rejected_files:
        batch.status = "partial"
    elif batch.accepted_files:
        batch.status = "uploaded"
    else:
        batch.status = "rejected"
    batch.save(update_fields=["total_files", "accepted_files", "rejected_files", "total_size", "status", "updated_at"])
    return batch


@transaction.atomic
def create_image_batch(owner, upload_type, files):
    if not files:
        raise ValidationError("At least one image file is required.")
    from django.conf import settings
    if len(files) > settings.MAX_UPLOAD_BATCH_FILES:
        raise ValidationError(f"A batch cannot contain more than {settings.MAX_UPLOAD_BATCH_FILES} images.")
    batch = UploadBatch.objects.create(owner=owner, upload_type=upload_type)
    for uploaded_file in files:
        try:
            _save_valid_image(batch, owner, uploaded_file)
        except ValidationError as error:
            _create_rejected(batch, owner, uploaded_file.name, _error_text(error), uploaded_file.size)
    return _finalize_batch(batch)


@transaction.atomic
def create_zip_batch(owner, uploaded_zip):
    members = validate_zip_archive(uploaded_zip)
    batch = UploadBatch.objects.create(
        owner=owner,
        upload_type="zip",
        original_archive_name=safe_original_name(uploaded_zip.name),
    )
    uploaded_zip.seek(0)
    with zipfile.ZipFile(uploaded_zip) as archive:
        for member in members:
            filename = safe_original_name(member.filename)
            with tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode="w+b") as temporary:
                try:
                    with archive.open(member) as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            temporary.write(chunk)
                    temporary.seek(0)
                    wrapped = File(temporary, name=filename)
                    _save_valid_image(batch, owner, wrapped, filename)
                except ValidationError as error:
                    _create_rejected(batch, owner, filename, _error_text(error), member.file_size)
    return _finalize_batch(batch)


@transaction.atomic
def delete_batch(batch):
    for image in list(batch.images.all()):
        image.delete()
    batch.delete()

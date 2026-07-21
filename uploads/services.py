import tempfile
import time
import re
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
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
            saved_image = _save_valid_image(batch, owner, uploaded_file)
        except ValidationError as error:
            _create_rejected(batch, owner, uploaded_file.name, _error_text(error), uploaded_file.size)
            continue
        if upload_type == "single":
            try:
                preprocess_image(saved_image, requested_format="auto")
            except ValidationError:
                # Preserve the accepted original for inspection and retry.
                continue
    batch = _finalize_batch(batch)
    if upload_type == "single" and batch.accepted_files:
        completed = ImagePreprocessingResult.objects.filter(
            image__batch=batch,
            status="completed",
        ).count()
        batch.processing_status = "completed" if completed else "failed"
        batch.processed_files = completed
        batch.processing_failed_files = batch.accepted_files - completed
        batch.processing_started_at = timezone.now()
        batch.processing_finished_at = timezone.now()
        batch.processing_error = "" if completed else "The image could not be preprocessed."
        batch.save(
            update_fields=[
                "processing_status", "processed_files", "processing_failed_files",
                "processing_started_at", "processing_finished_at", "processing_error", "updated_at",
            ]
        )
    return batch


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
                    saved_image = _save_valid_image(batch, owner, wrapped, filename)
                except ValidationError as error:
                    _create_rejected(batch, owner, filename, _error_text(error), member.file_size)
                    continue
    return _finalize_batch(batch)


@transaction.atomic
def delete_batch(batch):
    for image in list(batch.images.all()):
        image.delete()
    batch.delete()


def safe_output_stem(value, fallback):
    raw = Path(str(value or "").replace("\\", "/")).name
    stem = Path(raw).stem.strip() or fallback
    stem = re.sub(r"[^A-Za-z0-9 _.-]+", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._-")
    return (stem[:100] or fallback)


def _has_transparency(image):
    if image.mode in {"RGBA", "LA"}:
        return image.getchannel("A").getextrema()[0] < 255
    if image.mode == "P" and "transparency" in image.info:
        return True
    return False


def _prepare_color_mode(image, output_format):
    if output_format == "PNG":
        if _has_transparency(image):
            return image.convert("RGBA")
        return image.convert("RGB")

    if _has_transparency(image):
        rgba = image.convert("RGBA")
        try:
            background_color = ImageColor.getrgb(settings.PREPROCESS_TRANSPARENT_BACKGROUND)
        except ValueError as error:
            raise ValidationError("The configured transparency background color is invalid.") from error
        background = Image.new("RGB", rgba.size, background_color)
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _select_output_format(requested_format, had_transparency, original_format):
    if requested_format == "png":
        return "PNG"
    if requested_format == "jpeg":
        return "JPEG"
    if original_format == "PNG":
        return "PNG"
    if original_format == "JPEG":
        return "JPEG"
    return "PNG" if had_transparency else "JPEG"


@transaction.atomic
def preprocess_image(image_upload, requested_format="auto", requested_name="", force=False):
    image_upload = (
        ImageUpload.objects.select_for_update()
        .select_related("owner")
        .get(pk=image_upload.pk)
    )
    if image_upload.status != "uploaded" or not image_upload.original_file:
        raise ValidationError("Only successfully uploaded images can be preprocessed.")
    if requested_format not in {"auto", "png", "jpeg"}:
        raise ValidationError({"output_format": "Choose auto, png, or jpeg."})

    fallback_stem = safe_output_stem(image_upload.original_name, f"image_{image_upload.pk}")
    requested_stem = safe_output_stem(requested_name, fallback_stem)
    existing = ImagePreprocessingResult.objects.filter(image=image_upload).first()
    if (
        existing
        and existing.status == "completed"
        and existing.requested_format == requested_format
        and existing.requested_name == requested_stem
        and not force
        and existing.processed_file
    ):
        if not hasattr(image_upload, "face_validation"):
            from validation.services import validate_image_faces_safely
            validate_image_faces_safely(image_upload)
        if settings.DUPLICATE_AUTO_ANALYZE and not hasattr(
            image_upload, "duplicate_analysis"
        ):
            from duplicates.services import enqueue_duplicate_analysis
            enqueue_duplicate_analysis(image_upload)
        if not hasattr(image_upload, "quality_decision"):
            from quality_control.services import enqueue_quality_analysis
            enqueue_quality_analysis(image_upload)
        return existing, False

    started = time.perf_counter()
    temporary_name = None
    output = None
    old_file_name = existing.processed_file.name if existing and existing.processed_file else None
    result = existing or ImagePreprocessingResult(image=image_upload, owner=image_upload.owner)

    if not existing:
        result.requested_format = requested_format
        result.requested_name = requested_stem
        result.status = "processing"
        result.save()

    try:
        image_upload.original_file.open("rb")
        with Image.open(image_upload.original_file) as source:
            source.load()
            original_format = source.format or image_upload.file_extension.lstrip(".").upper()
            original_size = source.size
            had_transparency = _has_transparency(source)
            working = ImageOps.exif_transpose(source)
            output_format = _select_output_format(requested_format, had_transparency, original_format)
            working = _prepare_color_mode(working, output_format)
            working.thumbnail(
                (settings.PREPROCESS_MAX_WIDTH, settings.PREPROCESS_MAX_HEIGHT),
                Image.Resampling.LANCZOS,
            )
            extension = ".png" if output_format == "PNG" else ".jpg"
            output_name = f"{requested_stem}{extension}"
            output = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode="w+b")
            save_options = {"optimize": True}
            if output_format == "JPEG":
                save_options["quality"] = settings.PREPROCESS_JPEG_QUALITY
            else:
                save_options["compress_level"] = 6
            working.save(output, format=output_format, **save_options)
            output_size = output.tell()
            output.seek(0)

            result.processed_file.save(output_name, File(output, name=output_name), save=False)
            temporary_name = result.processed_file.name
            result.owner = image_upload.owner
            result.status = "completed"
            result.requested_format = requested_format
            result.requested_name = requested_stem
            result.original_format = original_format.lower()
            result.output_format = output_format.lower()
            result.output_name = output_name
            result.original_width, result.original_height = original_size
            result.output_width, result.output_height = working.size
            result.original_file_size = image_upload.file_size
            result.processed_file_size = output_size
            result.had_transparency = had_transparency
            result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            result.failure_reason = ""
            result.save()
        from validation.services import validate_image_faces_safely
        validate_image_faces_safely(image_upload)
        if settings.DUPLICATE_AUTO_ANALYZE:
            from duplicates.services import enqueue_duplicate_analysis
            enqueue_duplicate_analysis(image_upload, force=force)
        from quality_control.services import enqueue_quality_analysis
        enqueue_quality_analysis(image_upload)
        if old_file_name and old_file_name != result.processed_file.name:
            result.processed_file.storage.delete(old_file_name)
        return result, True
    except ValidationError:
        if temporary_name:
            result.processed_file.storage.delete(temporary_name)
        if not existing:
            result.status = "failed"
            result.failure_reason = "Image preprocessing configuration is invalid."
            result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            result.save(update_fields=["status", "failure_reason", "processing_duration_ms", "updated_at"])
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        if temporary_name:
            result.processed_file.storage.delete(temporary_name)
        if not existing:
            result.status = "failed"
            result.failure_reason = "The stored image could not be safely preprocessed."
            result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            result.save(update_fields=["status", "failure_reason", "processing_duration_ms", "updated_at"])
        raise ValidationError("The stored image could not be safely preprocessed.") from error
    except Exception as error:
        if temporary_name:
            result.processed_file.storage.delete(temporary_name)
        if not existing:
            result.status = "failed"
            result.failure_reason = "Image preprocessing failed unexpectedly."
            result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            result.save(update_fields=["status", "failure_reason", "processing_duration_ms", "updated_at"])
        raise ValidationError("Image preprocessing failed unexpectedly.") from error
    finally:
        if output is not None:
            output.close()
        image_upload.original_file.close()


def enqueue_batch_preprocessing(batch):
    from .tasks import preprocess_batch_task

    if batch.accepted_files == 0:
        batch.processing_status = "failed"
        batch.processing_error = "The batch contains no accepted images to process."
        batch.processing_finished_at = timezone.now()
        batch.save(update_fields=["processing_status", "processing_error", "processing_finished_at", "updated_at"])
        return batch

    task_id = uuid.uuid4().hex
    UploadBatch.objects.filter(pk=batch.pk).update(
        processing_status="queued",
        processing_task_id=task_id,
        processing_error="",
        processing_started_at=None,
        processing_finished_at=None,
        processed_files=0,
        processing_failed_files=0,
    )
    try:
        preprocess_batch_task.apply_async(args=[batch.pk], task_id=task_id)
    except Exception as error:
        UploadBatch.objects.filter(pk=batch.pk).update(
            processing_status="failed",
            processing_error="The background processing queue is unavailable.",
            processing_finished_at=timezone.now(),
        )
    batch.refresh_from_db()
    return batch

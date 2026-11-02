import logging
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
from PIL import Image, ImageColor, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from .decoding import to_working_colour
from .enlarge import enlarge_small_image
from .models import BatchImageProgress, ImagePreprocessingResult, ImageUpload, UploadBatch
from .validators import safe_original_name, validate_image_file, validate_zip_archive

logger = logging.getLogger(__name__)


class BatchHasNoFailedImages(ValidationError):
    default_detail = "This batch has no failed images to retry."


class BatchRetryLimitReached(ValidationError):
    pass


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
    if image.gps_lat is not None:
        from places.service import attach_place
        attach_place(image)                                       # a named place when the offline gazetteer is installed and approved; never raises
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
def create_image_batch(owner, upload_type, files, organization=None, auto_enhance=False):
    if not files:
        raise ValidationError("At least one image file is required.")
    from django.conf import settings
    if len(files) > settings.MAX_UPLOAD_BATCH_FILES:
        raise ValidationError(f"A batch cannot contain more than {settings.MAX_UPLOAD_BATCH_FILES} images.")
    from organizations.services import get_default_organization
    batch = UploadBatch.objects.create(
        owner=owner, upload_type=upload_type, organization=organization or get_default_organization(owner), auto_enhance=bool(auto_enhance),
    )
    from notifications.events import notify_upload_received
    transaction.on_commit(
        lambda batch_id=batch.pk: notify_upload_received(batch_id)
    )
    from activity.services import record_activity_event
    transaction.on_commit(
        lambda: record_activity_event(
            owner, "upload", "upload_received",
            object_type="UploadBatch", object_id=batch.pk,
            details={"upload_type": upload_type},
        )
    )
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
def create_zip_batch(owner, uploaded_zip, organization=None, auto_enhance=False):
    members = validate_zip_archive(uploaded_zip)
    from organizations.services import get_default_organization
    batch = UploadBatch.objects.create(
        owner=owner,
        upload_type="zip",
        original_archive_name=safe_original_name(uploaded_zip.name),
        organization=organization or get_default_organization(owner),
        auto_enhance=bool(auto_enhance),
    )
    from notifications.events import notify_upload_received

    transaction.on_commit(
        lambda batch_id=batch.pk: notify_upload_received(batch_id)
    )
    from activity.services import record_activity_event
    transaction.on_commit(
        lambda: record_activity_event(
            owner, "upload", "upload_received",
            object_type="UploadBatch", object_id=batch.pk,
            details={"upload_type": "zip"},
        )
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
                    continue
    return _finalize_batch(batch)


@transaction.atomic
def delete_batch(batch):
    from .deletion import delete_images

    with transaction.atomic():
        delete_images(ImageUpload.all_objects.filter(batch=batch))   # the batch's photographs in the trash go with it
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
        elif not hasattr(image_upload, "identity_analysis"):
            # Person folders follow the quality verdict (identities/verdict.py); one that is already decided and was never grouped is grouped now.
            from identities.verdict import group_after_verdict
            group_after_verdict(image_upload)
        if hasattr(image_upload, "quality_decision"):
            # Already decided and never enhanced automatically (a retry, a re-analysis): started now if its verdict allows it, and never twice.
            from enhancement.auto import enhance_after_verdict
            enhance_after_verdict(image_upload)
            from search.semantic.service import index_after_verdict
            index_after_verdict(image_upload)
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
            working, decoding = to_working_colour(source)
            if decoding["colour_profile"].get("error"):
                logger.warning("Image %s: %s", image_upload.pk, decoding["colour_profile"]["error"])
            output_format = _select_output_format(requested_format, had_transparency, original_format)
            working = _prepare_color_mode(working, output_format)
            working.thumbnail(
                (settings.PREPROCESS_MAX_WIDTH, settings.PREPROCESS_MAX_HEIGHT),
                Image.Resampling.LANCZOS,
            )
            working, enlargement = enlarge_small_image(working)   # a very small photograph is enlarged here (off unless SMALL_IMAGE_ENLARGE_ENABLED)
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
            result.enlargement = enlargement
            result.processing_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            result.failure_reason = ""
            result.save()
        from validation.services import validate_image_faces_safely
        validate_image_faces_safely(image_upload)
        if settings.DUPLICATE_AUTO_ANALYZE:
            from duplicates.services import enqueue_duplicate_analysis
            enqueue_duplicate_analysis(image_upload, force=force)
        # No identity analysis here: a photograph is filed into a person folder only once its quality verdict allows it, and that is queued from where
        # the verdict is saved (quality_control.services.process_quality_decision, identities/verdict.py).
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
    except Exception:
        UploadBatch.objects.filter(pk=batch.pk).update(
            processing_status="failed",
            processing_error="The background processing queue is unavailable.",
            processing_finished_at=timezone.now(),
        )
    batch.refresh_from_db()
    return batch


FINISHED_PROCESSING_STATUSES = ("completed", "partial", "failed", "cancelled")


def cancel_batch(batch):
    """Mark ``batch`` (and every non-terminal image in it) cancelled.

    A batch that has already finished (``completed``, ``partial``, ``failed`` or ``cancelled``) is returned unchanged: cancelling it again is
    harmless, and it keeps its real outcome.

    Extracted from the owner-facing cancel endpoint so the org-scoped
    moderator cancel endpoint (T-007) can call the exact same, already-used
    logic rather than duplicating it.
    """
    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.processing_status in FINISHED_PROCESSING_STATUSES:
            # Nothing is running, so there is nothing to cancel: relabelling a finished batch "cancelled" would contradict its photos.
            return locked
        locked.cancellation_requested = True
        locked.processing_status = "cancelled"
        locked.save(update_fields=["cancellation_requested", "processing_status", "updated_at"])
        BatchImageProgress.objects.filter(batch=locked).exclude(
            status__in=["accepted", "rejected", "needs_review", "completed", "failed", "cancelled"],
        ).update(
            stage="completed", status="cancelled", progress_percentage=100,
            failure_reason="Processing was cancelled by the user.",
            finished_at=timezone.now(),
        )
    locked.refresh_from_db()
    return locked


def retry_obstacle(batch, failed_count):
    """Why ``batch`` cannot be retried right now, as the error ``retry_batch`` raises for it; ``None`` when a retry would go ahead.

    The one place that decides it, so what a screen shows ("Retry" available or not) and what the retry does can never disagree.
    """
    if not failed_count:
        return BatchHasNoFailedImages()
    maximum = getattr(settings, "BATCH_PROGRESS_MAX_RETRIES", 3)
    if batch.retry_count >= maximum:
        return BatchRetryLimitReached(f"The maximum of {maximum} batch retries has been reached.")
    return None


def retry_batch(batch):
    """Requeue every failed image in ``batch`` from wherever it failed.

    Used by both the owner-facing retry endpoint and the administrator's, so
    both go through the exact same, already-tested logic. Raises
    ``BatchHasNoFailedImages`` or ``BatchRetryLimitReached`` when it cannot
    proceed; on success returns the refreshed batch.
    """
    from enhancement.models import EnhancementStatus
    from enhancement.tasks import process_enhancement_image
    from quality_control.services import enqueue_quality_analysis
    from validation.services import validate_image_faces_safely
    from .progress import sync_image_progress

    failed = list(
        BatchImageProgress.objects.filter(batch=batch, status="failed").select_related("image")
    )
    obstacle = retry_obstacle(batch, len(failed))
    if obstacle is not None:
        raise obstacle

    batch.cancellation_requested = False
    batch.retry_count += 1
    batch.processing_status = "queued"
    batch.save(update_fields=["cancellation_requested", "retry_count", "processing_status", "updated_at"])

    needs_preprocessing = False
    for progress in failed:
        progress.retry_count += 1
        progress.status = "queued"
        progress.failure_reason = ""
        progress.finished_at = None
        progress.save(update_fields=["retry_count", "status", "failure_reason", "finished_at", "updated_at"])
        image = progress.image
        if progress.failure_stage == "preprocessing":
            ImagePreprocessingResult.objects.filter(image=image).update(
                status="pending", failure_reason="",
            )
            needs_preprocessing = True
        elif progress.failure_stage == "face_validation":
            validate_image_faces_safely(image)
            enqueue_quality_analysis(image)
        elif progress.failure_stage == "quality_analysis":
            enqueue_quality_analysis(image)
        elif progress.failure_stage == "enhancement":
            result = image.enhancement_results.filter(status=EnhancementStatus.FAILED).order_by("-created_at").first()
            if result:
                task_id = uuid.uuid4().hex
                result.status = EnhancementStatus.QUEUED
                result.task_id = task_id
                result.failure_reason = ""
                result.finished_at = None
                result.save(update_fields=["status", "task_id", "failure_reason", "finished_at", "updated_at"])
                process_enhancement_image.apply_async(args=[result.pk], task_id=task_id)
        sync_image_progress(image.pk)

    if needs_preprocessing:
        enqueue_batch_preprocessing(batch)
    batch.refresh_from_db()
    return batch

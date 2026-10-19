import logging
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils import timezone

from enhancement.models import ImageEnhancementResult
from uploads.models import ImageUpload, UploadBatch

from .models import ProcessingReport

logger = logging.getLogger(__name__)

TERMINAL_BATCH_STATUSES = {"completed", "partial", "failed", "cancelled"}
TERMINAL_PROGRESS_STATUSES = {
    "accepted", "rejected", "needs_review", "completed", "failed", "cancelled",
}


class ReportNotReadyError(ValueError):
    pass


def _optional_related(instance, attribute):
    try:
        return getattr(instance, attribute)
    except ObjectDoesNotExist:
        return None


def _round(value):
    return None if value is None else round(float(value), 2)


def _confidence(value):
    if value is None:
        return None
    value = float(value)
    return round(value * 100 if 0 <= value <= 1 else value, 2)


def _image_queryset():
    return (
        ImageUpload.objects.select_related(
            "batch",
            "preprocessing_result",
            "face_validation",
            "quality_decision",
            "duplicate_analysis",
            "identity_analysis",
            "processing_progress",
        )
        .prefetch_related(
            Prefetch(
                "enhancement_results",
                queryset=ImageEnhancementResult.objects.order_by("-created_at"),
                to_attr="report_enhancement_results",
            ),
            "identity_faces__memberships__identity_group",
        )
    )


def get_report_image(image_id, owner_id):
    return _image_queryset().get(pk=image_id, owner_id=owner_id)


def get_report_batch(batch_id, owner_id):
    return (
        UploadBatch.objects.filter(owner_id=owner_id)
        .prefetch_related(
            Prefetch("images", queryset=_image_queryset(), to_attr="report_images")
        )
        .get(pk=batch_id)
    )


def ensure_image_report_ready(image):
    if image.status == "rejected":
        return
    preprocessing = _optional_related(image, "preprocessing_result")
    quality = _optional_related(image, "quality_decision")
    progress = _optional_related(image, "processing_progress")
    if preprocessing and preprocessing.status == "failed":
        return
    if quality and quality.status in {"completed", "failed"}:
        return
    if progress and progress.status in TERMINAL_PROGRESS_STATUSES:
        return
    if image.batch.processing_status in TERMINAL_BATCH_STATUSES:
        return
    raise ReportNotReadyError(
        "Image processing must reach a terminal state before generating a report."
    )


def ensure_batch_report_ready(batch):
    if batch.processing_status not in TERMINAL_BATCH_STATUSES:
        raise ReportNotReadyError(
            "Batch processing must reach a terminal state before generating a report."
        )


def _rejection_reasons(image, preprocessing, face, quality, progress):
    reasons = []
    if image.rejection_reason:
        reasons.append(image.rejection_reason)
    if preprocessing and preprocessing.failure_reason:
        reasons.append(preprocessing.failure_reason)
    if face and face.rejection_reason:
        reasons.append(face.rejection_reason)
    if quality:
        reasons.extend(quality.rejection_reasons or [])
    if progress and progress.failure_reason:
        reasons.append(progress.failure_reason)
    return list(dict.fromkeys(str(reason) for reason in reasons if reason))


def _image_status(image, preprocessing, enhancement, quality, progress):
    if image.status == "rejected":
        return "rejected"
    if progress and progress.status in TERMINAL_PROGRESS_STATUSES:
        return progress.status
    if quality and quality.decision:
        return quality.decision
    if quality and quality.status == "failed":
        return "failed"
    if enhancement and enhancement.status in {"completed", "failed", "cancelled"}:
        return enhancement.status
    if preprocessing and preprocessing.status == "failed":
        return "failed"
    return image.status


def _identity_report_summary(image, identity):
    if identity is None:
        return {"available": False, "group_id": None, "confidence": None}
    memberships = []
    for face in image.identity_faces.all():
        for membership in face.memberships.all():
            if membership.state in {"auto_matched", "new_group", "confirmed", "needs_review"}:
                memberships.append({
                    "group_id": membership.identity_group_id,
                    "display_name": membership.identity_group.display_name if membership.identity_group_id else None,
                    "face_index": face.face_index,
                    "confidence": _confidence(membership.similarity_score),
                    "state": membership.state,
                })
    first = memberships[0] if memberships else {}
    return {
        "available": identity.status in {"completed", "needs_review"},
        "group_id": first.get("group_id"),
        "confidence": first.get("confidence"),
        "status": identity.status,
        "groups": memberships,
    }


def build_image_record(image):
    preprocessing = _optional_related(image, "preprocessing_result")
    face = _optional_related(image, "face_validation")
    quality = _optional_related(image, "quality_decision")
    duplicate = _optional_related(image, "duplicate_analysis")
    identity = _optional_related(image, "identity_analysis")
    progress = _optional_related(image, "processing_progress")
    enhancement_results = getattr(image, "report_enhancement_results", None)
    if enhancement_results is None:
        enhancement_results = list(image.enhancement_results.order_by("-created_at")[:1])
    enhancement = enhancement_results[0] if enhancement_results else None

    sharpness_score = _round(quality.sharpness_score) if quality else None
    blur_score = None if sharpness_score is None else round(
        max(0.0, min(100.0, 100.0 - sharpness_score)), 2
    )
    parameters = enhancement.applied_parameters if enhancement else {}
    algorithm = parameters.get("algorithm") if isinstance(parameters, dict) else None
    links = {
        "original": (
            f"/api/images/{image.pk}/download/" if image.original_file else None
        ),
        "standardized": (
            f"/api/images/{image.pk}/preprocessing/download/"
            if preprocessing and preprocessing.processed_file else None
        ),
        "enhanced": (
            f"/api/enhancements/results/{enhancement.pk}/download/"
            if enhancement and enhancement.status == "completed" and enhancement.enhanced_file
            else None
        ),
    }
    durations = [
        getattr(preprocessing, "processing_duration_ms", 0),
        getattr(face, "processing_duration_ms", 0),
        getattr(quality, "processing_duration_ms", 0),
        getattr(enhancement, "processing_duration_ms", 0),
        getattr(duplicate, "processing_duration_ms", 0),
        getattr(identity, "processing_duration_ms", 0),
    ]
    reasons = _rejection_reasons(image, preprocessing, face, quality, progress)
    failure_candidates = [
        getattr(progress, "failure_reason", ""),
        getattr(quality, "failure_reason", ""),
        getattr(enhancement, "failure_reason", ""),
        getattr(preprocessing, "failure_reason", ""),
    ]
    return {
        "image_id": image.pk,
        "batch_id": image.batch_id,
        "filename": image.original_name,
        "status": _image_status(image, preprocessing, enhancement, quality, progress),
        "rejection_reasons": reasons,
        "failure_reason": next((value for value in failure_candidates if value), None),
        "uploaded_at": image.created_at.isoformat(),
        "original": {
            "format": image.file_extension.lstrip(".") or None,
            "mime_type": image.mime_type or None,
            "width": image.width,
            "height": image.height,
            "file_size": image.file_size,
        },
        "preprocessing": {
            "status": preprocessing.status if preprocessing else None,
            "output_format": preprocessing.output_format if preprocessing else None,
            "output_width": preprocessing.output_width if preprocessing else None,
            "output_height": preprocessing.output_height if preprocessing else None,
            "output_file_size": preprocessing.processed_file_size if preprocessing else None,
        },
        "enhancement": {
            "result_id": enhancement.pk if enhancement else None,
            "status": enhancement.status if enhancement else None,
            "requested_level": enhancement.requested_level if enhancement else None,
            "applied_level": enhancement.applied_level if enhancement else None,
            "algorithm": algorithm,
            "output_format": enhancement.output_format if enhancement else None,
            "output_width": enhancement.output_width if enhancement else None,
            "output_height": enhancement.output_height if enhancement else None,
            "output_file_size": enhancement.output_file_size if enhancement else None,
        },
        "quality": {
            "status": quality.status if quality else None,
            "decision": quality.decision if quality else None,
            "overall_score": _round(quality.overall_score) if quality else None,
            "brightness_score": _round(quality.brightness_score) if quality else None,
            "sharpness_score": sharpness_score,
            "blur_score": blur_score,
            "contrast_score": _round(quality.contrast_score) if quality else None,
            "noise_score": _round(quality.noise_score) if quality else None,
            "exposure_score": _round(quality.exposure_score) if quality else None,
            "resolution_score": _round(quality.resolution_score) if quality else None,
            "policy_version": quality.policy_version if quality else None,
        },
        "face_validation": {
            "decision": face.decision if face else None,
            "face_count": face.face_count if face else None,
            "primary_confidence": _confidence(face.primary_confidence) if face else None,
            "detector": face.detector if face else None,
        },
        "duplicate_analysis": {
            "status": duplicate.status if duplicate else None,
            "decision": duplicate.decision if duplicate else None,
            "match_count": duplicate.match_count if duplicate else 0,
            "policy_version": duplicate.policy_version if duplicate else None,
        },
        "identity_group": _identity_report_summary(image, identity),
        "processing_duration_ms": sum(int(value or 0) for value in durations),
        "protected_links": links,
    }


def build_report_payload(report):
    generated_at = timezone.now().isoformat()
    if report.report_type == ProcessingReport.ReportType.IMAGE:
        image = get_report_image(report.image_id, report.owner_id)
        ensure_image_report_ready(image)
        record = build_image_record(image)
        return {
            "schema_version": report.schema_version,
            "report_type": "image",
            "generated_at": generated_at,
            "source": {
                "image_id": image.pk,
                "batch_id": image.batch_id,
                "name": image.original_name,
            },
            "summary": {"total_images": 1, "status": record["status"]},
            "images": [record],
        }

    batch = get_report_batch(report.batch_id, report.owner_id)
    ensure_batch_report_ready(batch)
    records = [build_image_record(image) for image in batch.report_images]
    counts = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    # The batch keeps its own totals after photographs are deleted (deleting photographs does not touch them), so the totals come from the batch and the
    # photographs that no longer exist are counted as such: the numbers always add up and "Total images" is never 0 next to accepted uploads.
    total_images = max(batch.total_files, batch.accepted_files + batch.rejected_files, len(records))
    deleted_images = total_images - len(records)
    if deleted_images:
        counts["deleted"] = deleted_images
    enhanced_images = (
        sum(1 for record in records if record["enhancement"]["status"] == "completed") if records else None   # unknown once every photograph is gone
    )
    return {
        "schema_version": report.schema_version,
        "report_type": "batch",
        "generated_at": generated_at,
        "source": {
            "batch_id": batch.pk,
            "name": batch.display_name or batch.original_archive_name or f"Batch {batch.pk}",
            "upload_type": batch.upload_type,
            "created_at": batch.created_at.isoformat(),
        },
        "summary": {
            "total_images": total_images,
            "accepted_uploads": batch.accepted_files,
            "rejected_uploads": batch.rejected_files,
            "processing_status": batch.processing_status,
            "status_counts": counts,
            "total_input_size": batch.total_size,
            "images_deleted": deleted_images > 0,
            "images_remaining": len(records),
            "enhanced_images": enhanced_images,
        },
        "images": records,
    }


def enqueue_processing_report(*, owner, report_format, image=None, batch=None):
    from .tasks import generate_processing_report_task

    if (image is None) == (batch is None):
        raise ValueError("Exactly one report source is required.")
    if image is not None:
        ensure_image_report_ready(image)
        report_type = ProcessingReport.ReportType.IMAGE
        source_filters = {"image": image, "batch__isnull": True}
    else:
        ensure_batch_report_ready(batch)
        report_type = ProcessingReport.ReportType.BATCH
        source_filters = {"batch": batch, "image__isnull": True}

    active_filters = {
        "owner": owner,
        "format": report_format,
        "status__in": [
            ProcessingReport.Status.QUEUED,
            ProcessingReport.Status.GENERATING,
        ],
        **source_filters,
    }
    try:
        with transaction.atomic():
            active = (
                ProcessingReport.objects.select_for_update()
                .filter(**active_filters)
                .first()
            )
            if active:
                return active, False
            task_id = uuid.uuid4().hex
            report = ProcessingReport.objects.create(
                owner=owner,
                report_type=report_type,
                format=report_format,
                image=image,
                batch=batch,
                status=ProcessingReport.Status.QUEUED,
                task_id=task_id,
            )

            def dispatch():
                try:
                    generate_processing_report_task.apply_async(
                        args=[report.pk], task_id=task_id
                    )
                except Exception:
                    logger.exception("Could not queue processing report_id=%s.", report.pk)
                    ProcessingReport.objects.filter(pk=report.pk).update(
                        status=ProcessingReport.Status.FAILED,
                        failure_reason="The report-generation queue is unavailable.",
                        generated_at=timezone.now(),
                    )

            transaction.on_commit(dispatch)
    except IntegrityError:
        # A concurrent request may create the same active report after the
        # initial lookup. The database constraint is the final source of truth.
        active = ProcessingReport.objects.filter(**active_filters).first()
        if active:
            return active, False
        raise
    report.refresh_from_db()
    return report, True

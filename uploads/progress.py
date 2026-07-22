from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone

from .models import BatchImageProgress, ImageUpload, UploadBatch


TERMINAL_STATUSES = {"accepted", "rejected", "needs_review", "completed", "failed", "cancelled"}
STAGE_PROGRESS = {
    "uploaded": 10.0, "queued": 12.0, "preprocessing": 30.0,
    "face_validation": 45.0, "quality_analysis": 65.0,
    "enhancement": 95.0, "finalizing": 98.0, "completed": 100.0,
}


def ensure_image_progress(image):
    progress, _ = BatchImageProgress.objects.get_or_create(
        image=image,
        defaults={"owner": image.owner, "batch": image.batch},
    )
    return progress


def _state_for_image(image):
    if image.batch.cancellation_requested:
        return "completed", "cancelled", 100.0, "", "Processing was cancelled by the user."
    if image.status == "rejected":
        return "completed", "rejected", 100.0, "uploaded", image.rejection_reason

    enhancement = image.enhancement_results.order_by("-created_at").first()
    if enhancement:
        if enhancement.status == "completed":
            return "completed", "completed", 100.0, "", ""
        if enhancement.status == "failed":
            return "enhancement", "failed", 95.0, "enhancement", enhancement.failure_reason
        if enhancement.status == "cancelled":
            return "completed", "cancelled", 100.0, "enhancement", enhancement.failure_reason
        return "enhancement", "processing" if enhancement.status == "processing" else "queued", 75.0, "", ""

    try:
        quality = image.quality_decision
    except Exception:
        quality = None
    if quality:
        if quality.status == "failed":
            return "quality_analysis", "failed", 65.0, "quality_analysis", quality.failure_reason
        if quality.status == "completed":
            decision = quality.decision or "needs_review"
            return "completed", decision, 100.0, "", ""
        return "quality_analysis", "processing" if quality.status == "processing" else "queued", 52.0, "", ""

    try:
        face = image.face_validation
    except Exception:
        face = None
    if face:
        if face.decision == "failed":
            return "face_validation", "failed", 45.0, "face_validation", face.message
        return "face_validation", "processing", 45.0, "", ""

    try:
        preprocessing = image.preprocessing_result
    except Exception:
        preprocessing = None
    if preprocessing:
        if preprocessing.status == "failed":
            return "preprocessing", "failed", 30.0, "preprocessing", preprocessing.failure_reason
        if preprocessing.status == "completed":
            return "face_validation", "processing", 35.0, "", ""
        if preprocessing.status == "processing":
            return "preprocessing", "processing", 20.0, "", ""
        return "preprocessing", "queued", 12.0, "", ""
    return "uploaded", "queued", 10.0, "", ""


def sync_image_progress(image_id):
    image = ImageUpload.objects.select_related("owner", "batch").get(pk=image_id)
    progress = ensure_image_progress(image)
    stage, status, percentage, failure_stage, failure_reason = _state_for_image(image)
    now = timezone.now()
    progress.stage = stage
    progress.status = status
    progress.progress_percentage = percentage
    progress.failure_stage = failure_stage
    progress.failure_reason = failure_reason or ""
    if status == "processing" and not progress.started_at:
        progress.started_at = now
    progress.finished_at = now if status in TERMINAL_STATUSES else None
    progress.save()
    recompute_batch_progress(image.batch_id)
    return progress


def recompute_batch_progress(batch_id):
    batch = UploadBatch.objects.get(pk=batch_id)
    for image in batch.images.all():
        ensure_image_progress(image)
    records = batch.image_progress.all()
    total = records.count()
    counts = {name: records.filter(status=name).count() for name in [
        "queued", "processing", "accepted", "rejected", "needs_review",
        "completed", "failed", "cancelled",
    ]}
    percentage = round(records.aggregate(value=Avg("progress_percentage"))["value"] or 0.0, 2)
    active = counts["queued"] + counts["processing"]
    if batch.cancellation_requested and not active:
        processing_status = "cancelled"
    elif active:
        processing_status = "processing" if counts["processing"] else "queued"
    elif counts["failed"] and counts["failed"] == total:
        processing_status = "failed"
    elif counts["failed"] or counts["rejected"] or counts["needs_review"]:
        processing_status = "partial"
    else:
        processing_status = "completed" if total else batch.processing_status
    active_record = records.exclude(status__in=TERMINAL_STATUSES).order_by("-progress_percentage").first()
    stage = active_record.stage if active_record else "completed"
    UploadBatch.objects.filter(pk=batch_id).update(
        current_stage=stage, processing_status=processing_status,
        progress_updated_at=timezone.now(),
    )
    if processing_status in {"completed", "partial", "failed", "cancelled"}:
        from notifications.events import notify_batch_completed
        notify_batch_completed(batch_id, counts)
    return {"total": total, "counts": counts, "percentage": percentage, "stage": stage, "status": processing_status}


def batch_progress_payload(batch):
    for image_id in batch.images.values_list("pk", flat=True):
        sync_image_progress(image_id)
    summary = recompute_batch_progress(batch.pk)
    batch.refresh_from_db()
    records = list(batch.image_progress.select_related("image").order_by("image_id"))
    durations = [(item.finished_at - item.started_at).total_seconds() for item in records if item.started_at and item.finished_at]
    remaining = summary["counts"]["queued"] + summary["counts"]["processing"]
    eta = timezone.now() + timedelta(seconds=(sum(durations) / len(durations)) * remaining) if durations and remaining else None
    return {
        "batch_id": batch.pk, "upload_type": batch.upload_type,
        "status": summary["status"], "stage": summary["stage"],
        "progress_percentage": summary["percentage"], "cancellation_requested": batch.cancellation_requested,
        "retry_count": batch.retry_count, "counts": summary["counts"],
        "estimated_completion_at": eta,
        "images": [{
            "image_id": item.image_id, "filename": item.image.original_name,
            "stage": item.stage, "status": item.status,
            "progress_percentage": item.progress_percentage,
            "failure_stage": item.failure_stage, "failure_reason": item.failure_reason,
            "retry_count": item.retry_count, "updated_at": item.updated_at,
        } for item in records],
        "updated_at": batch.progress_updated_at,
    }

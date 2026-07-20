from datetime import timedelta
from collections import Counter

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from enhancement.models import EnhancementStatus, ImageEnhancementResult
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult
from quality_control.models import ImageQualityDecision


def _daily_counts(queryset, date_field, *, distinct_field=None):
    count = Count(distinct_field or "id", distinct=bool(distinct_field))
    rows = (
        queryset.annotate(day=TruncDate(date_field))
        .values("day")
        .annotate(total=count)
    )
    return {row["day"]: row["total"] for row in rows}


def _recent_activity(user, limit=10):
    activities = []
    for image in ImageUpload.objects.filter(owner=user).order_by("-created_at")[:limit]:
        activities.append({
            "type": "image_rejected" if image.status == "rejected" else "image_uploaded",
            "message": (
                f"{image.original_name} was rejected during upload."
                if image.status == "rejected"
                else f"{image.original_name} was uploaded."
            ),
            "image_id": image.pk,
            "status": image.status,
            "created_at": image.created_at,
        })
    for result in ImageEnhancementResult.objects.filter(owner=user).select_related("image").order_by("-updated_at")[:limit]:
        activities.append({
            "type": f"enhancement_{result.status}",
            "message": f"Enhancement for {result.image.original_name} is {result.status}.",
            "image_id": result.image_id,
            "enhancement_id": result.pk,
            "status": result.status,
            "created_at": result.updated_at,
        })
    for result in FaceValidationResult.objects.filter(owner=user).select_related("image").order_by("-updated_at")[:limit]:
        activities.append({
            "type": f"face_validation_{result.decision}",
            "message": f"Face validation for {result.image.original_name}: {result.message}",
            "image_id": result.image_id,
            "validation_id": result.pk,
            "status": result.decision,
            "created_at": result.updated_at,
        })
    for result in ImageQualityDecision.objects.filter(owner=user).select_related("image").order_by("-updated_at")[:limit]:
        activities.append({
            "type": f"quality_{result.decision or result.status}",
            "message": f"Quality decision for {result.image.original_name}: {result.message or result.status}.",
            "image_id": result.image_id,
            "quality_id": result.pk,
            "status": result.decision or result.status,
            "created_at": result.updated_at,
        })
    return sorted(activities, key=lambda item: item["created_at"], reverse=True)[:limit]


def build_user_dashboard(user):
    images = ImageUpload.objects.filter(owner=user)
    enhancements = ImageEnhancementResult.objects.filter(owner=user)
    completed = enhancements.filter(status=EnhancementStatus.COMPLETED)
    validations = FaceValidationResult.objects.filter(owner=user)
    quality_results = ImageQualityDecision.objects.filter(owner=user, status="completed")
    active = enhancements.filter(status__in=[
        EnhancementStatus.PENDING,
        EnhancementStatus.QUEUED,
        EnhancementStatus.PROCESSING,
    ])
    failed_image_ids = set(enhancements.filter(status=EnhancementStatus.FAILED).values_list("image_id", flat=True))
    failed_image_ids.update(
        ImagePreprocessingResult.objects.filter(owner=user, status="failed").values_list("image_id", flat=True)
    )

    original_bytes = images.aggregate(total=Sum("file_size"))["total"] or 0
    standardized_bytes = ImagePreprocessingResult.objects.filter(owner=user).aggregate(
        total=Sum("processed_file_size")
    )["total"] or 0
    enhanced_bytes = enhancements.aggregate(total=Sum("output_file_size"))["total"] or 0

    today = timezone.localdate()
    start = today - timedelta(days=6)
    uploads_by_day = _daily_counts(images.filter(created_at__date__gte=start), "created_at")
    accepted_by_day = _daily_counts(
        quality_results.filter(decision="accepted", updated_at__date__gte=start),
        "updated_at",
        distinct_field="image_id",
    )
    rejected_by_day = _daily_counts(
        quality_results.filter(decision="rejected", updated_at__date__gte=start),
        "updated_at",
        distinct_field="image_id",
    )
    chart = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        chart.append({
            "date": day,
            "uploads": uploads_by_day.get(day, 0),
            "accepted": accepted_by_day.get(day, 0),
            "rejected": rejected_by_day.get(day, 0),
        })

    recent_uploads = [
        {
            "id": image.pk,
            "filename": image.original_name,
            "status": image.status,
            "batch_id": image.batch_id,
            "uploaded_at": image.created_at,
        }
        for image in images.order_by("-created_at")[:5]
    ]
    rejection_counts = Counter(
        reason
        for reasons in quality_results.filter(decision="rejected").values_list(
            "rejection_reasons", flat=True
        )
        for reason in reasons
    )
    return {
        "summary": {
            "total_images": images.count(),
            "upload_batches": UploadBatch.objects.filter(owner=user).count(),
            "enhanced_images": completed.values("image_id").distinct().count(),
            "accepted_images": quality_results.filter(decision="accepted").count(),
            "rejected_images": (
                images.filter(status="rejected").count()
                + quality_results.filter(decision="rejected", image__status="uploaded").count()
            ),
            "needs_review_images": quality_results.filter(decision="needs_review").count(),
            "average_quality_score": round(
                sum(
                    score for score in quality_results.values_list("overall_score", flat=True)
                    if score is not None
                ) / max(quality_results.exclude(overall_score__isnull=True).count(), 1),
                2,
            ),
            "most_common_rejection_reason": (
                rejection_counts.most_common(1)[0][0] if rejection_counts else None
            ),
            "processing_images": len(set(active.values_list("image_id", flat=True)) | set(
                ImageQualityDecision.objects.filter(
                    owner=user, status__in=["pending", "queued", "processing"]
                ).values_list("image_id", flat=True)
            )),
            "failed_images": len(failed_image_ids | set(
                ImageQualityDecision.objects.filter(owner=user, status="failed").values_list(
                    "image_id", flat=True
                )
            )),
            "identity_groups": 0,
            "identity_groups_available": False,
            "storage_bytes": original_bytes + standardized_bytes + enhanced_bytes,
        },
        "uploads_last_7_days": chart,
        "recent_uploads": recent_uploads,
        "recent_activity": _recent_activity(user),
    }

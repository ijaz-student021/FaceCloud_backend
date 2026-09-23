from .models import UserNotification
from .services import create_user_notification


def notify_upload_received(batch_id):
    from uploads.models import UploadBatch

    batch = UploadBatch.objects.select_related("owner").filter(pk=batch_id).first()
    if batch is None:
        return None
    count = batch.total_files
    notification, _ = create_user_notification(
        recipient=batch.owner,
        event_type=UserNotification.EventType.UPLOAD_RECEIVED,
        idempotency_key=f"batch:{batch.pk}:upload_received",
        title="Upload received",
        message=f"FaceCloud received {count} image{'s' if count != 1 else ''} for processing.",
        severity=UserNotification.Severity.INFO,
        resource_type="batch",
        resource_id=batch.pk,
        action_path="/dashboard/uploads",
        metadata={"batch_id": batch.pk, "status": batch.status},
    )
    return notification

def notify_processing_started(batch_id):
    from uploads.models import UploadBatch

    batch = UploadBatch.objects.select_related("owner").filter(pk=batch_id).first()
    if batch is None:
        return None
    notification, _ = create_user_notification(
        recipient=batch.owner,
        event_type=UserNotification.EventType.PROCESSING_STARTED,
        idempotency_key=f"batch:{batch.pk}:processing_started",
        title="Processing started",
        message="FaceCloud started processing your uploaded images.",
        severity=UserNotification.Severity.INFO,
        resource_type="batch",
        resource_id=batch.pk,
        action_path="/dashboard/uploads",
        metadata={"batch_id": batch.pk, "status": batch.processing_status},
    )
    return notification


def notify_quality_result(decision):
    image = decision.image
    metadata = {
        "image_id": image.pk,
        "batch_id": image.batch_id,
        "filename": image.original_name,
        "status": decision.decision or decision.status,
    }
    common = {
        "recipient": decision.owner,
        "resource_type": "image",
        "resource_id": image.pk,
        "action_path": "/dashboard/images",
        "metadata": metadata,
    }
    if decision.status == decision.Status.FAILED:
        notification, _ = create_user_notification(
            **common,
            event_type=UserNotification.EventType.PHOTO_FAILED,
            idempotency_key=f"image:{image.pk}:quality_failed",
            title="Photo processing failed",
            message=f"{image.original_name} could not complete quality analysis.",
            severity=UserNotification.Severity.ERROR,
        )
        return notification
    if decision.status != decision.Status.COMPLETED:
        return None
    if decision.decision == decision.Decision.ACCEPTED:
        event_type = UserNotification.EventType.PHOTO_ACCEPTED
        title = "Photo accepted"
        message = f"{image.original_name} passed the quality and face checks."
        severity = UserNotification.Severity.SUCCESS
    elif decision.decision == decision.Decision.REJECTED:
        event_type = UserNotification.EventType.PHOTO_REJECTED
        title = "Photo rejected"
        message = decision.message or f"{image.original_name} did not pass the required checks."
        severity = UserNotification.Severity.ERROR
    else:
        event_type = UserNotification.EventType.PHOTO_NEEDS_REVIEW
        title = "Photo needs review"
        message = decision.message or f"{image.original_name} requires review."
        severity = UserNotification.Severity.WARNING
    notification, _ = create_user_notification(
        **common,
        event_type=event_type,
        idempotency_key=f"image:{image.pk}:quality:{decision.decision}",
        title=title,
        message=message,
        severity=severity,
    )
    return notification


def notify_enhancement_result(result):
    image = result.image
    if result.status == "completed":
        notification, _ = create_user_notification(
            recipient=result.owner,
            event_type=UserNotification.EventType.PHOTO_ACCEPTED,
            idempotency_key=f"image:{image.pk}:enhancement_completed",
            title="Photo enhanced",
            message=f"{image.original_name} was enhanced successfully.",
            severity=UserNotification.Severity.SUCCESS,
            resource_type="image",
            resource_id=image.pk,
            action_path="/dashboard/images",
            metadata={
                "image_id": image.pk,
                "batch_id": image.batch_id,
                "filename": image.original_name,
                "status": result.status,
            },
        )
        return notification
    if result.status == "failed":
        notification, _ = create_user_notification(
            recipient=result.owner,
            event_type=UserNotification.EventType.PHOTO_FAILED,
            idempotency_key=f"image:{image.pk}:enhancement_failed",
            title="Enhancement failed",
            message=f"{image.original_name} could not be enhanced safely.",
            severity=UserNotification.Severity.ERROR,
            resource_type="image",
            resource_id=image.pk,
            action_path="/dashboard/images",
            metadata={
                "image_id": image.pk,
                "batch_id": image.batch_id,
                "filename": image.original_name,
                "status": result.status,
            },
        )
        return notification
    return None


def notify_batch_completed(batch_id, counts=None):
    from uploads.models import UploadBatch

    batch = UploadBatch.objects.select_related("owner").filter(pk=batch_id).first()
    if batch is None or batch.processing_status not in {
        "completed", "partial", "failed", "cancelled",
    }:
        return None
    counts = counts or {}
    accepted = int(counts.get("accepted", 0) + counts.get("completed", 0))
    rejected = int(counts.get("rejected", batch.rejected_files))
    review = int(counts.get("needs_review", 0))
    failed = int(counts.get("failed", batch.processing_failed_files))
    if batch.processing_status == "failed":
        severity = UserNotification.Severity.ERROR
        title = "Batch processing failed"
    elif batch.processing_status in {"partial", "cancelled"}:
        severity = UserNotification.Severity.WARNING
        title = "Batch processing finished with warnings"
    else:
        severity = UserNotification.Severity.SUCCESS
        title = "Batch processing completed"
    message = (
        f"Batch {batch.pk} finished: {accepted} accepted, {rejected} rejected, "
        f"{review} needing review and {failed} failed."
    )
    notification, _ = create_user_notification(
        recipient=batch.owner,
        event_type=UserNotification.EventType.BATCH_COMPLETED,
        idempotency_key=f"batch:{batch.pk}:terminal:{batch.processing_status}",
        title=title,
        message=message,
        severity=severity,
        resource_type="batch",
        resource_id=batch.pk,
        action_path="/dashboard/uploads",
        metadata={
            "batch_id": batch.pk,
            "status": batch.processing_status,
            "accepted": accepted,
            "rejected": rejected,
            "needs_review": review,
            "failed": failed,
        },
    )
    return notification


def notify_report_result(report):
    ready = report.status == report.Status.READY
    if report.status not in {report.Status.READY, report.Status.FAILED}:
        return None
    notification, _ = create_user_notification(
        recipient=report.owner,
        event_type=(
            UserNotification.EventType.REPORT_READY
            if ready else UserNotification.EventType.REPORT_FAILED
        ),
        idempotency_key=f"report:{report.pk}:{report.status}",
        title="Processing report ready" if ready else "Processing report failed",
        message=(
            f"Your {report.format.upper()} processing report is ready to download."
            if ready else "Your processing report could not be generated safely."
        ),
        severity=(
            UserNotification.Severity.SUCCESS if ready else UserNotification.Severity.ERROR
        ),
        resource_type="report",
        resource_id=report.pk,
        action_path="/dashboard/downloads",
        metadata={"report_id": report.pk, "status": report.status, "format": report.format},
    )
    return notification


def notify_download_result(package):
    ready = package.status == package.Status.READY
    if package.status not in {package.Status.READY, package.Status.FAILED}:
        return None
    notification, _ = create_user_notification(
        recipient=package.owner,
        event_type=(
            UserNotification.EventType.DOWNLOAD_READY
            if ready else UserNotification.EventType.DOWNLOAD_FAILED
        ),
        idempotency_key=f"download-package:{package.pk}:{package.status}",
        title="Download package ready" if ready else "Download package failed",
        message=(
            "Your batch download package is ready."
            if ready else "Your batch download package could not be generated safely."
        ),
        severity=(
            UserNotification.Severity.SUCCESS if ready else UserNotification.Severity.ERROR
        ),
        resource_type="download_package",
        resource_id=package.pk,
        action_path="/dashboard/downloads",
        metadata={
            "package_id": package.pk,
            "batch_id": package.batch_id,
            "status": package.status,
            "format": package.report_format,
        },
    )
    return notification


def notify_identity_export_result(export):
    ready = export.status == export.Status.READY
    if export.status not in {export.Status.READY, export.Status.FAILED}:
        return None
    notification, _ = create_user_notification(
        recipient=export.owner,
        event_type=(
            UserNotification.EventType.DOWNLOAD_READY
            if ready else UserNotification.EventType.DOWNLOAD_FAILED
        ),
        idempotency_key=f"identity-export:{export.pk}:{export.status}",
        title="Identity export ready" if ready else "Identity export failed",
        message=(
            "Your identity-group ZIP export is ready."
            if ready else "Your identity-group ZIP export could not be generated safely."
        ),
        severity=(
            UserNotification.Severity.SUCCESS if ready else UserNotification.Severity.ERROR
        ),
        resource_type="identity_export",
        resource_id=export.pk,
        action_path="/dashboard/person-folders",
        metadata={
            "export_id": export.pk,
            "group_id": export.identity_group_id,
            "status": export.status,
        },
    )
    return notification

from .models import UserNotification
from .services import create_user_notification

# Where a notification's "open" link goes. These are routes of the frontend's user portal
# (src/App.tsx): "Your uploads" is /dashboard/jobs and person folders are /dashboard/groups.
JOBS_PATH = "/dashboard/jobs"
GROUPS_PATH = "/dashboard/groups"


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
        action_path=JOBS_PATH,
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
        action_path=JOBS_PATH,
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
    from quality_control.effective import effective_decision, tips_for

    # Where no moderator can decide a "needs review" verdict (a personal workspace) the photograph is accepted, with a tip, not "needs review".
    if decision.decision == decision.Decision.NEEDS_REVIEW and effective_decision(image, decision.decision) == "accepted":
        tips = tips_for(decision.rejection_reasons)
        notification, _ = create_user_notification(
            **common,
            event_type=UserNotification.EventType.PHOTO_ACCEPTED,
            idempotency_key=f"image:{image.pk}:quality:accepted",
            title="Photo accepted",
            message=f"{image.original_name} was accepted." + (f" Tip: {tips[0]}" if tips else ""),
            severity=UserNotification.Severity.SUCCESS,
        )
        return notification
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


def notify_moderator_review_assigned(review):
    if review.assigned_moderator_id is None:
        return None
    notification, _ = create_user_notification(
        recipient=review.assigned_moderator,
        event_type=UserNotification.EventType.MODERATOR_REVIEW_ASSIGNED,
        idempotency_key=f"review:{review.pk}:assigned:{review.assigned_moderator_id}",
        title="A review was assigned to you",
        message=f"You were assigned a {review.get_source_display().lower()} review to decide.",
        severity=UserNotification.Severity.INFO,
        resource_type="moderator_review",
        resource_id=review.pk,
        action_path="/moderator/queue",
        metadata={
            "review_id": review.pk, "organization_id": review.organization_id, "source": review.source,
        },
    )
    return notification


def notify_moderator_review_escalated(review):
    from organizations.models import OrganizationMembership

    owners = OrganizationMembership.objects.filter(
        organization_id=review.organization_id, role=OrganizationMembership.Role.OWNER,
        status=OrganizationMembership.Status.ACTIVE,
    ).select_related("user")
    notifications = []
    for membership in owners:
        notification, _ = create_user_notification(
            recipient=membership.user,
            event_type=UserNotification.EventType.MODERATOR_REVIEW_ESCALATED,
            idempotency_key=f"review:{review.pk}:escalated:{membership.user_id}",
            title="A review was escalated",
            message=f"A {review.get_source_display().lower()} review was escalated and needs your attention.",
            severity=UserNotification.Severity.WARNING,
            resource_type="moderator_review",
            resource_id=review.pk,
            action_path="/moderator/queue",
            metadata={
                "review_id": review.pk, "organization_id": review.organization_id, "source": review.source,
            },
        )
        if notification:
            notifications.append(notification)
    return notifications


def notify_moderators_batch_failed(batch):
    """Tell an organization's owners and moderators that one of its batches failed entirely.

    The uploader already gets their own notification, so they are skipped here.
    The retry count is part of the key, so a batch that fails again after a
    retry alerts again while a repeated check of the same failure does not.
    """
    if batch.organization_id is None:
        return []
    from organizations.scope import moderators_who_see

    # Owners, and the moderators who may see this member's data (one who sees only their own members is not told about a batch of someone else's).
    staff = [
        membership for membership in moderators_who_see(batch.organization, batch.owner_id) if membership.user_id != batch.owner_id
    ]
    notifications = []
    for membership in staff:
        notification, _ = create_user_notification(
            recipient=membership.user,
            event_type=UserNotification.EventType.MODERATOR_BATCH_FAILED,
            idempotency_key=f"batch:{batch.pk}:failed:{batch.retry_count}:{membership.user_id}",
            title="A batch failed",
            message=f"Batch {batch.pk} in your organization failed to process. You can retry or cancel it.",
            severity=UserNotification.Severity.ERROR,
            resource_type="batch",
            resource_id=batch.pk,
            action_path="/moderator/batches",
            metadata={"batch_id": batch.pk, "organization_id": batch.organization_id, "status": "failed"},
        )
        if notification:
            notifications.append(notification)
    return notifications


def notify_member_of_review_decision(review, actor):
    """Tell the member whose photograph a moderator decided on what was decided (approved or not accepted), in plain words.

    Sent for approve, reject and correct (a correction is an approval the moderator adjusted); never for keep_in_review, never to the
    moderator who decided, and never for the moderator's own photographs. The only free text the member sees is ``message_to_member``: the
    internal reason and notes are not part of it. The preferences ``photo_accepted`` / ``photo_rejected`` and ``email_notifications`` are respected by
    ``create_user_notification``. When a notification was created the organization's audit trail records it (target: the review).
    """
    from moderation.member_messages import message_for_member
    from moderation.models import ModeratorReview
    from organizations.models import OrganizationAuditEvent
    from organizations.services import record_audit_event

    if review.status not in (ModeratorReview.Status.APPROVED, ModeratorReview.Status.REJECTED, ModeratorReview.Status.CORRECTED):
        return None
    image = review.image
    owner = image.owner
    if owner.pk == actor.pk:
        return None
    rejected = review.status == ModeratorReview.Status.REJECTED
    outcome = "rejected" if rejected else "approved"
    notification, created = create_user_notification(
        recipient=owner,
        event_type=UserNotification.EventType.PHOTO_REVIEW_REJECTED if rejected else UserNotification.EventType.PHOTO_REVIEW_APPROVED,
        idempotency_key=f"review:{review.pk}:member:{outcome}",
        title="Your photo was not accepted" if rejected else "Your photo was approved",
        message=message_for_member(review, outcome),
        severity=UserNotification.Severity.WARNING if rejected else UserNotification.Severity.SUCCESS,
        resource_type="image",
        resource_id=image.pk,
        action_path=f"/dashboard/images/{image.pk}",
        metadata={"image_id": image.pk, "review_id": review.pk, "organization_id": review.organization_id, "filename": image.original_name},
    )
    if created and notification is not None:
        record_audit_event(
            review.organization, actor, OrganizationAuditEvent.Action.MEMBER_NOTIFIED_OF_REVIEW,
            target_type="ModeratorReview", target_id=review.pk, new_value={"outcome": outcome},
        )
    return notification


def notify_folders_may_match(batch, pairs):
    """Tell the owner of ``batch`` that some of their person folders may be the same person (once per batch; ``pairs`` as ``identities.suggestions`` returns them).

    In-app only, switched by the ``person_group_created`` preference. Nothing is merged: the message sends them to their person folders.
    """
    folders = {folder for pair in pairs for folder in (pair["source_group"], pair["target_group"])}
    if not folders:
        return None
    notification, _ = create_user_notification(
        recipient=batch.owner,
        event_type=UserNotification.EventType.IDENTITY_MERGE_SUGGESTED,
        idempotency_key=f"batch:{batch.pk}:folders-may-match",
        title="Some of your person folders may be the same person",
        message=f"{len(folders)} of your person folders may be the same person. Check them and merge the ones that are.",
        severity=UserNotification.Severity.INFO,
        resource_type="batch",
        resource_id=batch.pk,
        action_path="/dashboard/groups",
        metadata={"batch_id": batch.pk, "folder_count": len(folders), "suggested_merge_count": len(pairs)},
    )
    return notification


def notify_moderator_review_overdue(review):
    """Tell whoever can act on a review that it has passed its deadline.

    Goes to the assigned moderator when there is one -- an overdue review that
    belongs to somebody is their problem to answer for. An unassigned one goes
    to the organization's owners and moderators instead, because nobody has
    picked it up and that is exactly what makes it late.
    """

    if review.assigned_moderator_id is not None:
        recipients = [review.assigned_moderator]
    else:
        from organizations.scope import moderators_who_see

        recipients = [membership.user for membership in moderators_who_see(review.organization, review.image.owner_id)]
    notifications = []
    for recipient in recipients:
        notification, _ = create_user_notification(
            recipient=recipient,
            event_type=UserNotification.EventType.MODERATOR_REVIEW_OVERDUE,
            idempotency_key=f"review:{review.pk}:overdue:{recipient.pk}",
            title="A review is overdue",
            message=(
                f"A {review.get_source_display().lower()} review passed its "
                "review deadline and is still waiting for a decision."
            ),
            severity=UserNotification.Severity.WARNING,
            resource_type="moderator_review",
            resource_id=review.pk,
            action_path="/moderator/queue",
            metadata={
                "review_id": review.pk, "organization_id": review.organization_id,
                "source": review.source,
                "due_at": review.due_at.isoformat() if review.due_at else None,
            },
        )
        if notification:
            notifications.append(notification)
    return notifications


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


def notify_batch_enhanced(batch_id, counts):
    """Once per batch, when its automatic enhancements are all finished: "3 photos enhanced". Never one per photograph (enhancement/auto.py)."""
    from uploads.models import UploadBatch

    batch = UploadBatch.objects.select_related("owner").filter(pk=batch_id).first()
    if batch is None:
        return None
    enhanced, kept, failed, skipped = (int(counts.get(key, 0)) for key in ("enhanced", "kept", "failed", "skipped"))
    message = f"{enhanced} photo{'s' if enhanced != 1 else ''} enhanced"
    details = []
    if kept:
        details.append(f"{kept} kept as uploaded because enhancing them would not have helped")
    if failed:
        details.append(f"{failed} could not be enhanced")
    if skipped:
        details.append(f"{skipped} not enhanced")
    if details:
        message += " (" + "; ".join(details) + ")"
    notification, _ = create_user_notification(
        recipient=batch.owner,
        event_type=UserNotification.EventType.BATCH_ENHANCED,
        idempotency_key=f"batch:{batch.pk}:enhanced",
        title="Photos enhanced",
        message=message + ".",
        severity=UserNotification.Severity.WARNING if failed else UserNotification.Severity.SUCCESS,
        resource_type="batch",
        resource_id=batch.pk,
        action_path=JOBS_PATH,
        metadata={"batch_id": batch.pk, "enhanced": enhanced, "kept": kept, "failed": failed, "skipped": skipped, "total": int(counts.get("total", 0))},
    )
    return notification


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
        action_path=JOBS_PATH,
        metadata={
            "batch_id": batch.pk,
            "status": batch.processing_status,
            "accepted": accepted,
            "rejected": rejected,
            "needs_review": review,
            "failed": failed,
        },
    )
    if batch.processing_status == "failed":
        notify_moderators_batch_failed(batch)
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
        action_path=GROUPS_PATH,
        metadata={
            "export_id": export.pk,
            "group_id": export.identity_group_id,
            "status": export.status,
        },
    )
    return notification

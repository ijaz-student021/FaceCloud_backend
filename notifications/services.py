import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import NotificationPreference, UserNotification

logger = logging.getLogger(__name__)


EVENT_POLICY = {
    UserNotification.EventType.UPLOAD_RECEIVED: (None, False),
    UserNotification.EventType.PROCESSING_STARTED: (None, False),
    UserNotification.EventType.PHOTO_ACCEPTED: ("photo_accepted", True),
    UserNotification.EventType.PHOTO_REJECTED: ("photo_rejected", True),
    UserNotification.EventType.PHOTO_NEEDS_REVIEW: ("photo_rejected", True),
    UserNotification.EventType.PHOTO_FAILED: ("photo_rejected", True),
    UserNotification.EventType.BATCH_COMPLETED: ("batch_upload_finished", True),
    UserNotification.EventType.REPORT_READY: ("batch_upload_finished", True),
    UserNotification.EventType.REPORT_FAILED: ("batch_upload_finished", True),
    UserNotification.EventType.DOWNLOAD_READY: ("batch_upload_finished", True),
    UserNotification.EventType.DOWNLOAD_FAILED: ("batch_upload_finished", True),
    UserNotification.EventType.IDENTITY_GROUP_CREATED: ("person_group_created", True),
    UserNotification.EventType.LIVE_SESSION_ALERT: ("live_session_alerts", False),
    UserNotification.EventType.MODERATOR_REVIEW_ASSIGNED: ("moderator_review_updates", True),
    UserNotification.EventType.MODERATOR_REVIEW_ESCALATED: ("moderator_review_updates", True),
    # In-app only: a failed batch can be noisy, and the uploader already gets the email.
    UserNotification.EventType.MODERATOR_BATCH_FAILED: ("moderator_review_updates", False),
}


def get_notification_preferences(user):
    preferences, _ = NotificationPreference.objects.get_or_create(user=user)
    return preferences


def _safe_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    safe = {}
    for key, value in metadata.items():
        if key in {
            "filename", "batch_id", "image_id", "report_id", "package_id",
            "status", "accepted", "rejected", "needs_review", "failed", "format",
            "review_id", "organization_id", "source",
        } and isinstance(value, (str, int, float, bool, type(None))):
            safe[str(key)[:40]] = value
    return safe


def create_user_notification(
    *, recipient, event_type, idempotency_key, title, message,
    severity=UserNotification.Severity.INFO, resource_type="", resource_id=None,
    action_path="", metadata=None,
):
    if event_type not in EVENT_POLICY:
        raise ValueError("Unsupported notification event type.")
    preferences = get_notification_preferences(recipient)
    preference_field, email_capable = EVENT_POLICY[event_type]
    if preference_field and not getattr(preferences, preference_field):
        return None, False

    queue_email = bool(
        email_capable
        and settings.NOTIFICATION_EMAILS_ENABLED
        and preferences.email_notifications
        and recipient.email
        and recipient.is_email_verified
        and recipient.is_active
    )
    defaults = {
        "event_type": event_type,
        "severity": severity,
        "title": str(title)[:160],
        "message": str(message)[:500],
        "resource_type": str(resource_type)[:32],
        "resource_id": resource_id,
        "action_path": str(action_path)[:255],
        "metadata": _safe_metadata(metadata),
        "email_status": (
            UserNotification.EmailStatus.QUEUED
            if queue_email else UserNotification.EmailStatus.SKIPPED
        ),
    }
    with transaction.atomic():
        notification, created = UserNotification.objects.get_or_create(
            recipient=recipient,
            idempotency_key=str(idempotency_key)[:160],
            defaults=defaults,
        )
        if created and queue_email:
            transaction.on_commit(lambda: enqueue_notification_email(notification.pk))
    return notification, created


def enqueue_notification_email(notification_id):
    from .tasks import send_notification_email_task

    try:
        send_notification_email_task.delay(notification_id)
    except Exception:
        logger.exception("Could not queue notification email notification_id=%s.", notification_id)
        UserNotification.objects.filter(
            pk=notification_id,
            email_status=UserNotification.EmailStatus.QUEUED,
        ).update(
            email_status=UserNotification.EmailStatus.FAILED,
            email_failure_reason="The notification email queue is unavailable.",
        )


def mark_notification_read(notification):
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return notification

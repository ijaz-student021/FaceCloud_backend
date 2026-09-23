import logging
from datetime import timedelta
from html import escape
from urllib.parse import urljoin

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from .models import NotificationPreference, UserNotification

# Transient infrastructure failures only (dropped DB connections).
TRANSIENT_INFRASTRUCTURE_ERRORS = (OperationalError, InterfaceError)

logger = logging.getLogger(__name__)


def deliver_notification_email(notification_id):
    notification = UserNotification.objects.select_related("recipient").filter(
        pk=notification_id
    ).first()
    if notification is None:
        return "deleted"
    if notification.email_status != UserNotification.EmailStatus.QUEUED:
        return notification.email_status

    preferences, _ = NotificationPreference.objects.get_or_create(
        user=notification.recipient
    )
    user = notification.recipient
    if not (
        settings.NOTIFICATION_EMAILS_ENABLED
        and preferences.email_notifications
        and user.email
        and user.is_email_verified
        and user.is_active
    ):
        UserNotification.objects.filter(pk=notification_id).update(
            email_status=UserNotification.EmailStatus.SKIPPED,
            email_failure_reason="",
        )
        return UserNotification.EmailStatus.SKIPPED

    link = ""
    if notification.action_path:
        link = urljoin(
            settings.FRONTEND_URL.rstrip("/") + "/",
            notification.action_path.lstrip("/"),
        )
    text = notification.message
    if link:
        text = f"{text}\n\nOpen FaceCloud: {link}"
    html = f"<p>{escape(notification.message)}</p>"
    if link:
        html += f'<p><a href="{escape(link)}">Open FaceCloud</a></p>'
    email = EmailMultiAlternatives(
        subject=f"FaceCloud: {notification.title}",
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    email.attach_alternative(html, "text/html")
    email.send(fail_silently=False)
    UserNotification.objects.filter(pk=notification_id).update(
        email_status=UserNotification.EmailStatus.SENT,
        email_failure_reason="",
        email_sent_at=timezone.now(),
    )
    return UserNotification.EmailStatus.SENT


@shared_task(bind=True, name="notifications.send_email", acks_late=True)
def send_notification_email_task(self, notification_id):
    try:
        return deliver_notification_email(notification_id)
    except Exception as error:
        maximum = max(1, settings.NOTIFICATION_EMAIL_MAX_RETRIES)
        if self.request.retries + 1 >= maximum:
            logger.exception(
                "Notification email permanently failed notification_id=%s.", notification_id
            )
            UserNotification.objects.filter(pk=notification_id).update(
                email_status=UserNotification.EmailStatus.FAILED,
                email_failure_reason="The notification email could not be delivered.",
            )
            return UserNotification.EmailStatus.FAILED
        raise self.retry(
            exc=error,
            countdown=min(300, 30 * (2 ** self.request.retries)),
            max_retries=maximum - 1,
        )


def cleanup_expired_notifications(now=None):
    cutoff = (now or timezone.now()) - timedelta(
        days=settings.NOTIFICATION_RETENTION_DAYS
    )
    with transaction.atomic():
        deleted, _ = UserNotification.objects.filter(
            read_at__isnull=False,
            read_at__lt=cutoff,
        ).delete()
    return deleted


@shared_task(
    name="notifications.cleanup_expired",
    autoretry_for=TRANSIENT_INFRASTRUCTURE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def cleanup_expired_notifications_task():
    return {"deleted_notifications": cleanup_expired_notifications()}

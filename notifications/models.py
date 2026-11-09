from django.conf import settings
from django.db import models
from django.db.models import Q


class NotificationPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    photo_accepted = models.BooleanField(default=True)
    photo_rejected = models.BooleanField(default=True)
    batch_upload_finished = models.BooleanField(default=True)
    person_group_created = models.BooleanField(default=True)
    email_notifications = models.BooleanField(default=False)
    live_session_alerts = models.BooleanField(default=False)
    moderator_review_updates = models.BooleanField(default=True)
    # Not a notification: the one per-account setting there is. Whether new uploads are enhanced automatically once they pass the checks (a form field on an
    # upload overrides it for that upload). Kept here because this is the account's existing preferences endpoint.
    auto_enhance = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"notification-preferences:{self.user_id}"


class UserNotification(models.Model):
    class EventType(models.TextChoices):
        UPLOAD_RECEIVED = "upload_received", "Upload received"
        PROCESSING_STARTED = "processing_started", "Processing started"
        PHOTO_ACCEPTED = "photo_accepted", "Photo accepted"
        PHOTO_REJECTED = "photo_rejected", "Photo rejected"
        PHOTO_NEEDS_REVIEW = "photo_needs_review", "Photo needs review"
        PHOTO_FAILED = "photo_failed", "Photo processing failed"
        BATCH_COMPLETED = "batch_completed", "Batch completed"
        REPORT_READY = "report_ready", "Report ready"
        REPORT_FAILED = "report_failed", "Report failed"
        DOWNLOAD_READY = "download_ready", "Download ready"
        DOWNLOAD_FAILED = "download_failed", "Download failed"
        IDENTITY_GROUP_CREATED = "identity_group_created", "Identity group created"
        LIVE_SESSION_ALERT = "live_session_alert", "Live session alert"
        MODERATOR_REVIEW_ASSIGNED = "moderator_review_assigned", "Moderator review assigned"
        MODERATOR_REVIEW_ESCALATED = "moderator_review_escalated", "Moderator review escalated"
        MODERATOR_BATCH_FAILED = "moderator_batch_failed", "Organization batch failed"
        MODERATOR_REVIEW_OVERDUE = "moderator_review_overdue", "Moderator review overdue"
        PHOTO_REVIEW_APPROVED = "photo_review_approved", "Photo approved by a moderator"
        PHOTO_REVIEW_REJECTED = "photo_review_rejected", "Photo not accepted by a moderator"
        IDENTITY_MERGE_SUGGESTED = "identity_merge_suggested", "Person folders may be the same person"
        BATCH_ENHANCED = "batch_enhanced", "Photos enhanced"

    class Severity(models.TextChoices):
        INFO = "info", "Information"
        SUCCESS = "success", "Success"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    class EmailStatus(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    severity = models.CharField(
        max_length=12,
        choices=Severity.choices,
        default=Severity.INFO,
    )
    title = models.CharField(max_length=160)
    message = models.CharField(max_length=500)
    resource_type = models.CharField(max_length=32, blank=True)
    resource_id = models.PositiveBigIntegerField(null=True, blank=True)
    action_path = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=160)
    read_at = models.DateTimeField(null=True, blank=True)
    email_status = models.CharField(
        max_length=12,
        choices=EmailStatus.choices,
        default=EmailStatus.SKIPPED,
    )
    email_failure_reason = models.CharField(max_length=255, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["recipient", "event_type", "-created_at"]),
            models.Index(
                fields=["recipient", "-created_at"],
                condition=Q(read_at__isnull=True),
                name="notification_unread_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "idempotency_key"],
                name="notification_recipient_event_once",
            ),
        ]

    @property
    def is_read(self):
        return self.read_at is not None

    def __str__(self):
        return f"{self.recipient_id}:{self.event_type}:{self.id}"

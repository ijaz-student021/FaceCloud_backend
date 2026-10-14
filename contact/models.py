from django.db import models


class ContactMessage(models.Model):
    """A message from the public contact form, delivered to the team by email.

    Kept only for delivery and follow-up; deleted after
    CONTACT_MESSAGE_RETENTION_DAYS (contact.services.purge_old_messages).
    """

    class Reason(models.TextChoices):
        GENERAL = "General question", "General question"
        PARTNERSHIP = "Enterprise / Datamart partnership", "Enterprise / Datamart partnership"
        SUPPORT = "Technical support", "Technical support"
        ISSUE = "Report an issue", "Report an issue"
        OTHER = "Other", "Other"

    class Delivery(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=120)
    email = models.EmailField()
    company = models.CharField(max_length=150, blank=True)
    reason = models.CharField(max_length=64, choices=Reason.choices)
    message = models.TextField()
    delivery_status = models.CharField(max_length=8, choices=Delivery.choices, default=Delivery.QUEUED)
    delivery_attempts = models.PositiveSmallIntegerField(default=0)
    delivered_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

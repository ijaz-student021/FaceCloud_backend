from django.conf import settings
from django.db import models


class ActivityEvent(models.Model):
    class Category(models.TextChoices):
        UPLOAD = "upload", "Upload"
        PROCESSING = "processing", "Processing"
        MODERATION = "moderation", "Moderation"
        DOWNLOAD = "download", "Download"
        GROUP = "group", "Identity group"
        SHARE = "share", "Share"
        ADMIN = "admin", "Administration"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_events",
    )
    category = models.CharField(max_length=16, choices=Category.choices)
    event = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["category", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.category}:{self.event} ({self.user_id})"

from django.conf import settings
from django.db import models


class ConsentPurpose(models.TextChoices):
    FACE_IDENTITY = "face_identity", "Face identification and person grouping"


class BiometricConsent(models.Model):
    """Append-only consent history; the newest row per purpose is the current state."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="biometric_consents",
    )
    purpose = models.CharField(max_length=32, choices=ConsentPurpose.choices)
    granted = models.BooleanField()
    policy_version = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "purpose", "-created_at"])]

    def __str__(self):
        return f"{self.user_id}:{self.purpose}:{'granted' if self.granted else 'withdrawn'}"


class PrivacyRequest(models.Model):
    """Audit record of a privacy action. Holds counts only, never personal data."""

    class Kind(models.TextChoices):
        EXPORT = "export", "Data export"
        FACIAL_ERASURE = "facial_erasure", "Facial-data erasure"
        RETENTION_PURGE = "retention_purge", "Retention purge"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        USER = "user", "User request"
        CONSENT_WITHDRAWAL = "consent_withdrawal", "Consent withdrawal"
        RETENTION = "retention", "Retention policy"

    # SET_NULL so the audit trail survives account deletion without keeping identity.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="privacy_requests",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    trigger = models.CharField(max_length=24, choices=Trigger.choices, default=Trigger.USER)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMPLETED)
    summary = models.JSONField(default=dict, blank=True)
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["kind", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.kind}:{self.status}"

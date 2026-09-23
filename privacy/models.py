import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver


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
        KEY_ROTATION = "key_rotation", "Embedding key rotation"
        TEMP_CLEANUP = "temp_cleanup", "Temporary file cleanup"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        USER = "user", "User request"
        CONSENT_WITHDRAWAL = "consent_withdrawal", "Consent withdrawal"
        RETENTION = "retention", "Retention policy"
        OPERATOR = "operator", "Operator command"

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


def organization_export_upload_path(instance, filename):
    # Every FileField in the project uploads under users/<id>/<subdir>/<file>
    # (see privacy/cleanup.py's orphan scanner and its coverage tests), so
    # this is keyed by the requesting user, the same way DownloadPackage's
    # batch-wide archive is keyed by its owner rather than the batch.
    return f"users/{instance.requested_by_id}/organization-exports/{uuid.uuid4().hex}.json"


class OrganizationExport(models.Model):
    """An organization-owned counterpart to the personal export (build_privacy_export).

    Built async, unlike the personal export: it can span every batch, image
    and review across an organization rather than one user's own rows, so it
    follows the same job pattern as downloads.DownloadPackage /
    reports.ProcessingReport instead of returning inline. Deliberately
    excludes anything keyed to an individual member's personal account
    (consent, login, activity history) -- organization membership must never
    expose a member's personal content, the same rule every other org-scoped
    view in this codebase already follows.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="exports",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    export_file = models.FileField(upload_to=organization_export_upload_path, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    file_checksum_sha256 = models.CharField(max_length=64, blank=True)
    row_counts = models.JSONField(default=dict, blank=True)
    truncated = models.JSONField(default=dict, blank=True)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["organization"], condition=Q(status__in=["queued", "generating"]),
                name="privacy_one_active_org_export",
            ),
        ]

    def __str__(self):
        return f"export {self.pk} for org {self.organization_id} ({self.status})"


@receiver(post_delete, sender=OrganizationExport)
def delete_organization_export_file(sender, instance, **kwargs):
    if instance.export_file:
        instance.export_file.storage.delete(instance.export_file.name)

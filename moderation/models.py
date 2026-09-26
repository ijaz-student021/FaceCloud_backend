import uuid

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver


class ModeratorReview(models.Model):
    """One AI decision that landed in the ambiguous/uncertain band, queued for a human.

    Created by an additive hook next to the pipeline that produced the
    uncertain result (identities.services, quality_control.services,
    duplicates.services) -- never by rewriting that pipeline's own logic. See
    moderation/services.py's create_review_for_* functions.
    """

    class Source(models.TextChoices):
        QUALITY = "quality", "Quality"
        DUPLICATE = "duplicate", "Duplicate"
        IDENTITY = "identity", "Identity"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CORRECTED = "corrected", "Corrected"
        ESCALATED = "escalated", "Escalated"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="moderator_reviews",
    )
    image = models.ForeignKey(
        "uploads.ImageUpload", on_delete=models.CASCADE, related_name="moderator_reviews",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    ai_recommendation = models.CharField(max_length=32, blank=True)
    ai_confidence = models.FloatField(null=True, blank=True)
    # Exactly one of these is set, matching `source`. Kept as three specific
    # FKs rather than a generic relation so each is a real, queryable,
    # database-enforced link back to the record a decision writes into.
    quality_decision = models.ForeignKey(
        "quality_control.ImageQualityDecision", on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderator_reviews",
    )
    duplicate_match = models.ForeignKey(
        "duplicates.DuplicateMatch", on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderator_reviews",
    )
    identity_membership = models.ForeignKey(
        "identities.IdentityMembership", on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderator_reviews",
    )
    assigned_moderator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_moderator_reviews",
    )
    decision_reason = models.TextField(blank=True)
    decision_notes = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["quality_decision"], condition=models.Q(quality_decision__isnull=False),
                name="moderation_unique_quality_review",
            ),
            models.UniqueConstraint(
                fields=["duplicate_match"], condition=models.Q(duplicate_match__isnull=False),
                name="moderation_unique_duplicate_review",
            ),
            models.UniqueConstraint(
                fields=["identity_membership"], condition=models.Q(identity_membership__isnull=False),
                name="moderation_unique_identity_review",
            ),
        ]

    def __str__(self):
        return f"{self.source} review {self.pk} for org {self.organization_id}"


def moderator_report_upload_path(instance, filename):
    # Every FileField in the project uploads under users/<id>/<subdir>/<file>
    # (privacy/cleanup.py's orphan scanner and its coverage tests enforce
    # this), so this is keyed by the requesting user, same as
    # privacy.OrganizationExport.
    return f"users/{instance.requested_by_id}/moderator-reports/{uuid.uuid4().hex}.{instance.format}"


class ModeratorReport(models.Model):
    """An organization-scoped report, the async-job counterpart to reports.ProcessingReport.

    ProcessingReport has no organization FK (T-008 Phase B never touched
    reports/) and its renderers are hard-coded to a per-image data shape, so
    this follows privacy.OrganizationExport's job shape instead rather than
    reusing ProcessingReport -- see moderation/renderers.py for the shared
    table_style()/csv_safe() helpers this does reuse from reports/.
    """

    class ReportType(models.TextChoices):
        REVIEW_SUMMARY = "review_summary", "Review summary"
        MODERATOR_ACTIVITY = "moderator_activity", "Moderator activity"
        REJECTION_REASONS = "rejection_reasons", "Rejection reasons"
        QUALITY_SCORES = "quality_scores", "Quality scores"
        BATCHES = "batches", "Batches"
        DUPLICATE_DECISIONS = "duplicate_decisions", "Duplicate decisions"

    class Format(models.TextChoices):
        JSON = "json", "JSON"
        CSV = "csv", "CSV"
        PDF = "pdf", "PDF"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="moderator_reports",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    report_type = models.CharField(max_length=24, choices=ReportType.choices)
    format = models.CharField(max_length=8, choices=Format.choices, default=Format.JSON)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    report_file = models.FileField(upload_to=moderator_report_upload_path, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    content_type = models.CharField(max_length=64, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    file_checksum_sha256 = models.CharField(max_length=64, blank=True)
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
                fields=["organization", "report_type", "format"],
                condition=models.Q(status__in=["queued", "generating"]),
                name="moderation_one_active_report",
            ),
        ]

    def __str__(self):
        return f"{self.report_type} report {self.pk} for org {self.organization_id} ({self.status})"


@receiver(post_delete, sender=ModeratorReport)
def delete_moderator_report_file(sender, instance, **kwargs):
    if instance.report_file:
        instance.report_file.storage.delete(instance.report_file.name)

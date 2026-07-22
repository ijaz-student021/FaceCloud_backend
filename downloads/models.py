import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from uploads.models import UploadBatch


def download_package_upload_path(instance, filename):
    extension = Path(filename).suffix.lower() or ".zip"
    return f"users/{instance.owner_id}/download-packages/{uuid.uuid4().hex}{extension}"


class DownloadPackage(models.Model):
    class PackageType(models.TextChoices):
        ACCEPTED_RESULTS = "accepted_results", "Accepted enhanced results"
        COMPLETE_BATCH = "complete_batch", "Complete batch results"

    class ReportFormat(models.TextChoices):
        JSON = "json", "JSON"
        CSV = "csv", "CSV"
        PDF = "pdf", "PDF"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="download_packages",
    )
    batch = models.ForeignKey(
        UploadBatch,
        on_delete=models.CASCADE,
        related_name="download_packages",
    )
    package_type = models.CharField(max_length=24, choices=PackageType.choices)
    report_format = models.CharField(
        max_length=8,
        choices=ReportFormat.choices,
        default=ReportFormat.CSV,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    package_file = models.FileField(upload_to=download_package_upload_path, blank=True)
    display_name = models.CharField(max_length=255, blank=True)
    manifest = models.JSONField(default=dict, blank=True)
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
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"]),
            models.Index(fields=["owner", "package_type", "-created_at"]),
            models.Index(fields=["batch", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "batch", "package_type", "report_format"],
                condition=Q(status__in=["queued", "generating"]),
                name="download_one_active_batch_package",
            ),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.batch_id}:{self.package_type}:{self.status}"


class DownloadEvent(models.Model):
    class ResourceType(models.TextChoices):
        ORIGINAL_IMAGE = "original_image", "Original image"
        STANDARDIZED_IMAGE = "standardized_image", "Standardized image"
        ENHANCED_IMAGE = "enhanced_image", "Enhanced image"
        EDITED_IMAGE = "edited_image", "Edited image"
        PROCESSING_REPORT = "processing_report", "Processing report"
        BATCH_PACKAGE = "batch_package", "Batch package"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="download_events",
    )
    resource_type = models.CharField(max_length=24, choices=ResourceType.choices)
    resource_id = models.PositiveBigIntegerField()
    batch_id = models.PositiveBigIntegerField(null=True, blank=True)
    image_id = models.PositiveBigIntegerField(null=True, blank=True)
    filename = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=100, blank=True)
    downloaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-downloaded_at"]
        indexes = [
            models.Index(fields=["owner", "-downloaded_at"]),
            models.Index(fields=["owner", "resource_type", "-downloaded_at"]),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.resource_type}:{self.resource_id}"


@receiver(post_delete, sender=DownloadPackage)
def delete_download_package_file(sender, instance, **kwargs):
    if instance.package_file:
        instance.package_file.storage.delete(instance.package_file.name)

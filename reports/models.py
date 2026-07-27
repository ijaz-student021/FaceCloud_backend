import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from uploads.models import ImageUpload, UploadBatch


def report_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/reports/{uuid.uuid4().hex}{extension}"


class ProcessingReport(models.Model):
    class ReportType(models.TextChoices):
        IMAGE = "image", "Image"
        BATCH = "batch", "Batch"

    class Format(models.TextChoices):
        JSON = "json", "JSON"
        CSV = "csv", "CSV"
        PDF = "pdf", "PDF"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="processing_reports",
    )
    report_type = models.CharField(max_length=8, choices=ReportType.choices)
    format = models.CharField(max_length=8, choices=Format.choices)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    image = models.ForeignKey(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="processing_reports",
        null=True,
        blank=True,
    )
    batch = models.ForeignKey(
        UploadBatch,
        on_delete=models.CASCADE,
        related_name="processing_reports",
        null=True,
        blank=True,
    )
    schema_version = models.CharField(max_length=40, default="processing_report_v1")
    payload = models.JSONField(default=dict, blank=True)
    report_file = models.FileField(upload_to=report_upload_path, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    file_checksum_sha256 = models.CharField(max_length=64, blank=True)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"]),
            models.Index(fields=["owner", "report_type", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(image__isnull=False, batch__isnull=True, report_type="image")
                    | Q(image__isnull=True, batch__isnull=False, report_type="batch")
                ),
                name="report_exactly_one_matching_source",
            ),
            models.UniqueConstraint(
                fields=["owner", "image", "format"],
                condition=Q(
                    image__isnull=False,
                    status__in=["queued", "generating"],
                ),
                name="report_one_active_image_format",
            ),
            models.UniqueConstraint(
                fields=["owner", "batch", "format"],
                condition=Q(
                    batch__isnull=False,
                    status__in=["queued", "generating"],
                ),
                name="report_one_active_batch_format",
            ),
        ]

    @property
    def source_id(self):
        return self.image_id or self.batch_id

    def __str__(self):
        return f"{self.report_type}:{self.source_id}:{self.format}:{self.status}"


@receiver(post_delete, sender=ProcessingReport)
def delete_report_file(sender, instance, **kwargs):
    if instance.report_file:
        instance.report_file.storage.delete(instance.report_file.name)

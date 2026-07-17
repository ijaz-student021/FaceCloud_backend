import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch


def enhanced_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/enhanced/{uuid.uuid4().hex}{extension}"


class EnhancementLevel(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    AUTO = "auto", "Automatic"


class EnhancementStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    PARTIAL = "partial", "Partially completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class EnhancementBatchJob(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enhancement_batch_jobs",
    )
    source_batch = models.ForeignKey(
        UploadBatch,
        on_delete=models.CASCADE,
        related_name="enhancement_jobs",
    )
    requested_level = models.CharField(
        max_length=8,
        choices=EnhancementLevel.choices,
        default=EnhancementLevel.AUTO,
    )
    status = models.CharField(
        max_length=12,
        choices=EnhancementStatus.choices,
        default=EnhancementStatus.PENDING,
    )
    total_images = models.PositiveIntegerField(default=0)
    processed_images = models.PositiveIntegerField(default=0)
    successful_images = models.PositiveIntegerField(default=0)
    failed_images = models.PositiveIntegerField(default=0)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"]),
            models.Index(fields=["source_batch", "-created_at"]),
        ]

    @property
    def progress_percentage(self):
        if self.total_images == 0:
            return 0
        return min(100, round((self.processed_images / self.total_images) * 100, 2))


class ImageEnhancementResult(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="image_enhancement_results",
    )
    image = models.ForeignKey(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="enhancement_results",
    )
    preprocessing_result = models.ForeignKey(
        ImagePreprocessingResult,
        on_delete=models.CASCADE,
        related_name="enhancement_results",
    )
    batch_job = models.ForeignKey(
        EnhancementBatchJob,
        on_delete=models.CASCADE,
        related_name="results",
        null=True,
        blank=True,
    )
    requested_level = models.CharField(
        max_length=8,
        choices=EnhancementLevel.choices,
        default=EnhancementLevel.AUTO,
    )
    applied_level = models.CharField(
        max_length=8,
        choices=EnhancementLevel.choices,
        blank=True,
    )
    status = models.CharField(
        max_length=12,
        choices=EnhancementStatus.choices,
        default=EnhancementStatus.PENDING,
    )
    enhanced_file = models.FileField(upload_to=enhanced_upload_path, blank=True)
    output_name = models.CharField(max_length=255, blank=True)
    output_format = models.CharField(max_length=10, blank=True)
    output_width = models.PositiveIntegerField(null=True, blank=True)
    output_height = models.PositiveIntegerField(null=True, blank=True)
    output_file_size = models.PositiveBigIntegerField(default=0)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    applied_parameters = models.JSONField(default=dict, blank=True)
    failure_reason = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"]),
            models.Index(fields=["image", "-created_at"]),
            models.Index(fields=["batch_job", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["image"],
                condition=Q(status__in=["pending", "queued", "processing"]),
                name="enhancement_one_active_per_image",
            )
        ]


@receiver(post_delete, sender=ImageEnhancementResult)
def delete_enhanced_image(sender, instance, **kwargs):
    if instance.enhanced_file:
        instance.enhanced_file.storage.delete(instance.enhanced_file.name)

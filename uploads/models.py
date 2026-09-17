import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver


def original_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/originals/{uuid.uuid4().hex}{extension}"


def processed_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/preprocessed/{uuid.uuid4().hex}{extension}"


class UploadBatch(models.Model):
    TYPE_CHOICES = [
        ("single", "Single image"),
        ("multiple", "Multiple images"),
        ("zip", "ZIP archive"),
    ]
    STATUS_CHOICES = [
        ("uploaded", "Uploaded"),
        ("partial", "Partially accepted"),
        ("rejected", "Rejected"),
        ("failed", "Failed"),
    ]
    PROCESSING_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("queued", "Queued"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("partial", "Partially completed"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]
    STAGE_CHOICES = [
        ("uploaded", "Uploaded"), ("queued", "Queued"),
        ("preprocessing", "Preprocessing"), ("face_validation", "Face validation"),
        ("quality_analysis", "Quality analysis"), ("enhancement", "Enhancement"),
        ("identity_grouping", "Identity grouping"),
        ("finalizing", "Finalizing"), ("completed", "Completed"),
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_batches")
    upload_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    original_archive_name = models.CharField(max_length=255, blank=True)
    display_name = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="uploaded")
    total_files = models.PositiveIntegerField(default=0)
    accepted_files = models.PositiveIntegerField(default=0)
    rejected_files = models.PositiveIntegerField(default=0)
    total_size = models.PositiveBigIntegerField(default=0)
    processing_status = models.CharField(
        max_length=12,
        choices=PROCESSING_STATUS_CHOICES,
        default="pending",
    )
    processed_files = models.PositiveIntegerField(default=0)
    processing_failed_files = models.PositiveIntegerField(default=0)
    processing_task_id = models.CharField(max_length=255, blank=True, db_index=True)
    processing_error = models.TextField(blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_finished_at = models.DateTimeField(null=True, blank=True)
    current_stage = models.CharField(max_length=24, choices=STAGE_CHOICES, default="uploaded")
    cancellation_requested = models.BooleanField(default=False)
    retry_count = models.PositiveSmallIntegerField(default=0)
    progress_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class ImageUpload(models.Model):
    STATUS_CHOICES = [("uploaded", "Uploaded"), ("rejected", "Rejected")]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="image_uploads")
    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="images")
    original_file = models.FileField(upload_to=original_upload_path, blank=True)
    original_name = models.CharField(max_length=255)
    file_extension = models.CharField(max_length=10, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class ImagePreprocessingResult(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]
    FORMAT_CHOICES = [("auto", "Automatic"), ("png", "PNG"), ("jpeg", "JPEG")]

    image = models.OneToOneField(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="preprocessing_result",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="image_preprocessing_results",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    requested_format = models.CharField(max_length=8, choices=FORMAT_CHOICES, default="auto")
    requested_name = models.CharField(max_length=120, blank=True)
    original_format = models.CharField(max_length=20, blank=True)
    output_format = models.CharField(max_length=10, blank=True)
    output_name = models.CharField(max_length=255, blank=True)
    processed_file = models.FileField(upload_to=processed_upload_path, blank=True)
    original_width = models.PositiveIntegerField(null=True, blank=True)
    original_height = models.PositiveIntegerField(null=True, blank=True)
    output_width = models.PositiveIntegerField(null=True, blank=True)
    output_height = models.PositiveIntegerField(null=True, blank=True)
    original_file_size = models.PositiveBigIntegerField(default=0)
    processed_file_size = models.PositiveBigIntegerField(default=0)
    had_transparency = models.BooleanField(default=False)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class BatchImageProgress(models.Model):
    STATUS_CHOICES = [
        ("queued", "Queued"), ("processing", "Processing"),
        ("accepted", "Accepted"), ("rejected", "Rejected"),
        ("needs_review", "Needs review"), ("completed", "Completed"),
        ("failed", "Failed"), ("cancelled", "Cancelled"),
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="batch_image_progress")
    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="image_progress")
    image = models.OneToOneField(ImageUpload, on_delete=models.CASCADE, related_name="processing_progress")
    stage = models.CharField(max_length=24, choices=UploadBatch.STAGE_CHOICES, default="uploaded")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="queued")
    progress_percentage = models.FloatField(default=10.0)
    failure_stage = models.CharField(max_length=24, blank=True)
    failure_reason = models.TextField(blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["image_id"]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["batch", "stage"]),
        ]



@receiver(post_delete, sender=ImageUpload)
def delete_stored_image(sender, instance, **kwargs):
    if instance.original_file:
        instance.original_file.storage.delete(instance.original_file.name)


@receiver(post_delete, sender=ImagePreprocessingResult)
def delete_preprocessed_image(sender, instance, **kwargs):
    if instance.processed_file:
        instance.processed_file.storage.delete(instance.processed_file.name)

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver


def original_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/originals/{uuid.uuid4().hex}{extension}"


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

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_batches")
    upload_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    original_archive_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="uploaded")
    total_files = models.PositiveIntegerField(default=0)
    accepted_files = models.PositiveIntegerField(default=0)
    rejected_files = models.PositiveIntegerField(default=0)
    total_size = models.PositiveBigIntegerField(default=0)
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



@receiver(post_delete, sender=ImageUpload)
def delete_stored_image(sender, instance, **kwargs):
    if instance.original_file:
        instance.original_file.storage.delete(instance.original_file.name)

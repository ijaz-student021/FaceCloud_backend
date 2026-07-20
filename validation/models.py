from django.conf import settings
from django.db import models

from uploads.models import ImagePreprocessingResult, ImageUpload


class FaceValidationResult(models.Model):
    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        NEEDS_REVIEW = "needs_review", "Needs review"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="face_validation_results",
    )
    image = models.OneToOneField(
        ImageUpload, on_delete=models.CASCADE, related_name="face_validation",
    )
    preprocessing_result = models.ForeignKey(
        ImagePreprocessingResult, on_delete=models.CASCADE,
        related_name="face_validation_results",
    )
    decision = models.CharField(max_length=16, choices=Decision.choices)
    face_count = models.PositiveIntegerField(default=0)
    primary_confidence = models.FloatField(null=True, blank=True)
    faces = models.JSONField(default=list, blank=True)
    rejection_reason = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)
    detector = models.CharField(max_length=80, default="opencv_haar_frontalface_v1")
    processing_duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "decision", "-updated_at"])]

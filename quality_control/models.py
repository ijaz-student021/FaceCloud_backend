from django.conf import settings
from django.db import models

from uploads.models import ImagePreprocessingResult, ImageUpload
from validation.models import FaceValidationResult


class ImageQualityDecision(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        NEEDS_REVIEW = "needs_review", "Needs review"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="image_quality_decisions",
    )
    image = models.OneToOneField(
        ImageUpload, on_delete=models.CASCADE, related_name="quality_decision",
    )
    preprocessing_result = models.ForeignKey(
        ImagePreprocessingResult, on_delete=models.CASCADE,
        related_name="quality_decisions",
    )
    face_validation = models.ForeignKey(
        FaceValidationResult, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="quality_decisions",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    decision = models.CharField(max_length=16, choices=Decision.choices, blank=True)
    overall_score = models.FloatField(null=True, blank=True)
    brightness_score = models.FloatField(null=True, blank=True)
    sharpness_score = models.FloatField(null=True, blank=True)
    contrast_score = models.FloatField(null=True, blank=True)
    noise_score = models.FloatField(null=True, blank=True)
    exposure_score = models.FloatField(null=True, blank=True)
    resolution_score = models.FloatField(null=True, blank=True)
    measurements = models.JSONField(default=dict, blank=True)
    defects = models.JSONField(default=list, blank=True)
    rejection_reasons = models.JSONField(default=list, blank=True)
    message = models.TextField(blank=True)
    policy_version = models.CharField(max_length=40, default="quality_policy_v1")
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "decision", "-updated_at"])]

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from uploads.models import ImagePreprocessingResult, ImageUpload


class DuplicateAnalysis(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class Decision(models.TextChoices):
        NO_DUPLICATE = "no_duplicate", "No duplicate"
        DUPLICATE = "duplicate", "Duplicate"
        NEEDS_REVIEW = "needs_review", "Needs review"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="duplicate_analyses",
    )
    image = models.OneToOneField(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="duplicate_analysis",
    )
    preprocessing_result = models.ForeignKey(
        ImagePreprocessingResult,
        on_delete=models.CASCADE,
        related_name="duplicate_analyses",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
    )
    decision = models.CharField(max_length=16, choices=Decision.choices, blank=True)
    perceptual_hash = models.CharField(max_length=16, blank=True, db_index=True)
    face_fingerprints = models.JSONField(default=list, blank=True)
    fingerprint_provider = models.CharField(
        max_length=80,
        default="perceptual_face_region_v1",
    )
    match_count = models.PositiveIntegerField(default=0)
    policy_version = models.CharField(max_length=40, default="duplicate_policy_v1")
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["owner", "status", "-updated_at"]),
            models.Index(fields=["owner", "decision", "-updated_at"]),
        ]


class DuplicateMatch(models.Model):
    class MatchType(models.TextChoices):
        EXACT = "exact", "Exact checksum"
        PERCEPTUAL = "perceptual", "Perceptual image"
        FACE_REGION = "face_region", "Face region"

    class ReviewStatus(models.TextChoices):
        SUSPECTED = "suspected", "Suspected"
        CONFIRMED = "confirmed", "Confirmed duplicate"
        NOT_DUPLICATE = "not_duplicate", "Not a duplicate"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="duplicate_matches",
    )
    analysis = models.ForeignKey(
        DuplicateAnalysis,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    source_image = models.ForeignKey(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="duplicate_matches_as_source",
    )
    matched_image = models.ForeignKey(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="duplicate_matches_as_candidate",
    )
    match_type = models.CharField(max_length=16, choices=MatchType.choices)
    similarity_score = models.FloatField()
    hash_distance = models.PositiveSmallIntegerField(null=True, blank=True)
    face_similarity = models.FloatField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.SUSPECTED,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_duplicate_matches",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-similarity_score", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_image", "matched_image", "match_type"],
                name="duplicate_unique_image_pair_type",
            ),
            models.CheckConstraint(
                condition=~Q(source_image=F("matched_image")),
                name="duplicate_distinct_image_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "review_status", "-created_at"]),
            models.Index(fields=["source_image", "match_type"]),
            models.Index(fields=["matched_image", "match_type"]),
        ]

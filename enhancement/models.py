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


def edited_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/edited/{uuid.uuid4().hex}{extension}"


def preview_upload_path(instance, filename):
    """Previews and their cached sources live apart from originals and saved outputs."""
    extension = Path(filename).suffix.lower()
    owner_id = getattr(instance, "owner_id", None) or instance.enhancement_result.owner_id
    return f"users/{owner_id}/previews/{uuid.uuid4().hex}{extension}"


class EnhancementLevel(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    AUTO = "auto", "Automatic"


class EnhancementStyle(models.TextChoices):
    NATURAL = "natural", "Natural"
    DYNAMIC = "dynamic", "Dynamic"
    AI_MAX = "ai_max", "AI Enhance"


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
    requested_style = models.CharField(
        max_length=8,
        choices=EnhancementStyle.choices,
        default=EnhancementStyle.NATURAL,
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
    requested_style = models.CharField(
        max_length=8,
        choices=EnhancementStyle.choices,
        default=EnhancementStyle.NATURAL,
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
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    # Which task execution is enhancing this result right now, and when it last
    # reported in. Guards against one result being processed twice at once.
    claim_token = models.CharField(max_length=32, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    # The Celery task and attempt that made the claim. A later attempt of the
    # same task may resume it: the earlier attempt has necessarily ended.
    claim_task_id = models.CharField(max_length=255, blank=True, default="")
    claim_attempt = models.PositiveSmallIntegerField(default=0)
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


class EditMode(models.TextChoices):
    AUTO = "auto", "Automatic"
    MANUAL = "manual", "Manual"


class ImageEditVersion(models.Model):
    """A non-destructive, user-owned render derived from an enhanced result."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="image_edit_versions",
    )
    enhancement_result = models.ForeignKey(
        ImageEnhancementResult,
        on_delete=models.CASCADE,
        related_name="edit_versions",
    )
    mode = models.CharField(max_length=8, choices=EditMode.choices)
    requested_adjustments = models.JSONField(default=dict)
    applied_adjustments = models.JSONField(default=dict)
    status = models.CharField(
        max_length=12,
        choices=EnhancementStatus.choices,
        default=EnhancementStatus.PENDING,
    )
    edited_file = models.FileField(upload_to=edited_upload_path, blank=True)
    output_name = models.CharField(max_length=255, blank=True)
    output_format = models.CharField(max_length=10, blank=True)
    output_width = models.PositiveIntegerField(null=True, blank=True)
    output_height = models.PositiveIntegerField(null=True, blank=True)
    output_file_size = models.PositiveBigIntegerField(default=0)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    quality_metadata = models.JSONField(default=dict, blank=True)
    failure_reason = models.TextField(blank=True)
    is_current = models.BooleanField(default=False)
    # The version of the enhanced image this edit was made from (previews.source_version)
    # and, when saved from a preview, which one. Only one worker renders an edit.
    source_version = models.CharField(max_length=32, blank=True)
    from_preview = models.ForeignKey(
        "EditPreview", null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_edits",
    )
    claim_token = models.CharField(max_length=32, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["enhancement_result", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["enhancement_result"],
                condition=Q(status__in=["pending", "queued", "processing"]),
                name="enhancement_one_active_edit_per_result",
            )
        ]


@receiver(post_delete, sender=ImageEditVersion)
def delete_edited_image(sender, instance, **kwargs):
    if instance.edited_file:
        instance.edited_file.storage.delete(instance.edited_file.name)


class EditPreviewSource(models.Model):
    """A bounded, lossless copy of one version of an enhanced image, cached for previews.

    Decoded once from the enhanced file (one decode of its compression, no
    further generation) and reused by every preview of that version, so a slider
    does not decode a 24-megapixel JPEG per movement. Immutable. ``baseline_file``
    is the matching bounded copy of the unenhanced photo, made only when a
    preview needs the intensity blend.
    """

    enhancement_result = models.ForeignKey(
        ImageEnhancementResult, on_delete=models.CASCADE, related_name="preview_sources",
    )
    source_version = models.CharField(max_length=32)
    file = models.FileField(upload_to=preview_upload_path)
    baseline_file = models.FileField(upload_to=preview_upload_path, blank=True)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    has_alpha = models.BooleanField(default=False)
    full_width = models.PositiveIntegerField()
    full_height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["enhancement_result", "source_version"],
                                    name="enhancement_one_preview_source_per_version"),
        ]


class EditPreview(models.Model):
    """One rendered, revision-specific preview. Immutable: a newer revision never overwrites an older one.

    ``revision`` is chosen by the client and increases with each change it
    wants to see. The highest revision of a result is its latest; a response to
    an older revision that arrives late is stored under its own id and reported
    as not latest. Retrying the same revision with the same settings returns the
    stored preview; the same revision with different settings is a conflict.
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="edit_previews")
    enhancement_result = models.ForeignKey(
        ImageEnhancementResult, on_delete=models.CASCADE, related_name="edit_previews",
    )
    source = models.ForeignKey(EditPreviewSource, on_delete=models.CASCADE, related_name="previews")
    revision = models.PositiveIntegerField()
    settings_hash = models.CharField(max_length=64)
    mode = models.CharField(max_length=8, choices=EditMode.choices)
    requested_adjustments = models.JSONField(default=dict)
    applied_adjustments = models.JSONField(default=dict)
    render_metadata = models.JSONField(default=dict, blank=True)
    file = models.FileField(upload_to=preview_upload_path)
    output_format = models.CharField(max_length=10)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    file_size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-revision", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "enhancement_result", "revision"],
                                    name="enhancement_one_preview_per_revision"),
        ]


@receiver(post_delete, sender=EditPreview)
def delete_preview_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.storage.delete(instance.file.name)


@receiver(post_delete, sender=EditPreviewSource)
def delete_preview_source_files(sender, instance, **kwargs):
    for field in (instance.file, instance.baseline_file):
        if field:
            field.storage.delete(field.name)


def comparison_upload_path(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"users/{instance.comparison.owner_id}/comparisons/{uuid.uuid4().hex}{extension}"


class ModelComparison(models.Model):
    """One image run through several enhancement profiles, side by side (Feature 35)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="model_comparisons",
    )
    image = models.ForeignKey(
        ImageUpload,
        on_delete=models.CASCADE,
        related_name="model_comparisons",
    )
    requested_level = models.CharField(
        max_length=8,
        choices=EnhancementLevel.choices,
        default=EnhancementLevel.AUTO,
    )
    profiles = models.JSONField(default=list)
    status = models.CharField(
        max_length=12,
        choices=EnhancementStatus.choices,
        default=EnhancementStatus.PENDING,
    )
    reference_dimensions = models.JSONField(default=list, blank=True)
    recommended_profile = models.CharField(max_length=24, blank=True)
    recommendation_reason = models.TextField(blank=True)
    total_duration_ms = models.PositiveIntegerField(default=0)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "-created_at"])]


class ModelComparisonOutput(models.Model):
    """The result of one profile (or the untouched source, profile ``original``)."""

    class Outcome(models.TextChoices):
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    comparison = models.ForeignKey(
        ModelComparison,
        on_delete=models.CASCADE,
        related_name="outputs",
    )
    profile = models.CharField(max_length=24)
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    preview_file = models.FileField(upload_to=comparison_upload_path, blank=True)
    algorithm = models.CharField(max_length=200, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    scores = models.JSONField(default=dict, blank=True)
    overall_score = models.FloatField(null=True, blank=True)
    faces_detected = models.PositiveSmallIntegerField(null=True, blank=True)
    identity_guard = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["comparison", "profile"], name="comparison_one_output_per_profile",
            )
        ]


@receiver(post_delete, sender=ModelComparisonOutput)
def delete_comparison_preview(sender, instance, **kwargs):
    if instance.preview_file:
        instance.preview_file.storage.delete(instance.preview_file.name)

import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from uploads.models import ImagePreprocessingResult, ImageUpload
from validation.models import FaceValidationResult


def identity_export_upload_path(instance, filename):
    extension = Path(filename).suffix.lower() or ".zip"
    return f"users/{instance.owner_id}/identity-exports/{uuid.uuid4().hex}{extension}"


class IdentityAnalysis(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        NEEDS_REVIEW = "needs_review", "Needs review"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identity_analyses")
    image = models.OneToOneField(ImageUpload, on_delete=models.CASCADE, related_name="identity_analysis")
    preprocessing_result = models.ForeignKey(ImagePreprocessingResult, on_delete=models.CASCADE, related_name="identity_analyses")
    face_validation = models.ForeignKey(FaceValidationResult, on_delete=models.CASCADE, related_name="identity_analyses")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    face_count = models.PositiveSmallIntegerField(default=0)
    assigned_count = models.PositiveSmallIntegerField(default=0)
    review_count = models.PositiveSmallIntegerField(default=0)
    provider = models.CharField(max_length=80)
    model_checksum = models.CharField(max_length=64, blank=True)
    policy_version = models.CharField(max_length=40)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    processing_duration_ms = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "status", "-updated_at"])]


class IdentityFace(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identity_faces")
    image = models.ForeignKey(ImageUpload, on_delete=models.CASCADE, related_name="identity_faces")
    analysis = models.ForeignKey(IdentityAnalysis, on_delete=models.CASCADE, related_name="faces")
    face_index = models.PositiveSmallIntegerField()
    bounding_box = models.JSONField(default=dict)
    landmarks = models.JSONField(default=list, blank=True)
    protected_embedding = models.BinaryField(editable=False)
    embedding_dimension = models.PositiveIntegerField()
    provider = models.CharField(max_length=80)
    model_checksum = models.CharField(max_length=64, blank=True)
    quality = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["analysis_id", "face_index"]
        constraints = [models.UniqueConstraint(fields=["analysis", "face_index"], name="identity_unique_analysis_face")]
        indexes = [models.Index(fields=["owner", "image"])]

    def clean(self):
        if self.analysis_id and (self.owner_id != self.analysis.owner_id or self.image_id != self.analysis.image_id):
            raise ValidationError("Identity face ownership must match its analysis.")


class IdentityGroup(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        MERGED = "merged", "Merged"
        ARCHIVED = "archived", "Archived"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identity_groups")
    # See UploadBatch.organization: owner-scoped access is unchanged, this is
    # for the org-scoped moderator workflow (T-007) to query by directly. A
    # group has no single source image to derive it from transitively, so
    # unlike most identity models it needs its own column.
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="identity_groups",
    )
    display_name = models.CharField(max_length=80)
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    profile_membership = models.ForeignKey("IdentityMembership", on_delete=models.SET_NULL, null=True, blank=True, related_name="profile_for_groups")
    member_count = models.PositiveIntegerField(default=0)
    protected_centroid = models.BinaryField(editable=False, blank=True)
    embedding_dimension = models.PositiveIntegerField()
    provider = models.CharField(max_length=80)
    model_checksum = models.CharField(max_length=64, blank=True)
    policy_version = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name", "pk"]
        constraints = [models.UniqueConstraint(fields=["owner", "display_name"], name="identity_unique_owner_group_name")]
        indexes = [models.Index(fields=["owner", "state", "-updated_at"])]


class IdentityMembership(models.Model):
    class State(models.TextChoices):
        AUTO_MATCHED = "auto_matched", "Auto matched"
        NEW_GROUP = "new_group", "New group"
        NEEDS_REVIEW = "needs_review", "Needs review"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        REMOVED = "removed", "Removed"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identity_memberships")
    identity_face = models.ForeignKey(IdentityFace, on_delete=models.CASCADE, related_name="memberships")
    identity_group = models.ForeignKey(IdentityGroup, on_delete=models.CASCADE, related_name="memberships", null=True, blank=True)
    similarity_score = models.FloatField(null=True, blank=True)
    matching_distance = models.FloatField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=State.choices)
    decision_method = models.CharField(max_length=80)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_identity_memberships")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["identity_face"], condition=Q(state__in=["auto_matched", "new_group", "confirmed"]), name="identity_one_active_membership_per_face"),
            models.CheckConstraint(condition=Q(identity_group__isnull=False) | Q(state="needs_review"), name="identity_group_required_except_review"),
        ]
        indexes = [models.Index(fields=["owner", "state", "-created_at"]), models.Index(fields=["identity_group", "state"])]

    def clean(self):
        if self.identity_face_id and self.owner_id != self.identity_face.owner_id:
            raise ValidationError("Identity membership ownership must match its face.")
        if self.identity_group_id and self.owner_id != self.identity_group.owner_id:
            raise ValidationError("Identity membership ownership must match its group.")


class IdentityChange(models.Model):
    class Action(models.TextChoices):
        RENAMED = "renamed", "Renamed"
        MERGED = "merged", "Merged"
        MOVED = "moved", "Moved"
        REMOVED = "removed", "Removed"
        PROFILE_SET = "profile_set", "Profile set"
        DELETED = "deleted", "Deleted"
        REVIEWED = "reviewed", "Reviewed"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identity_changes")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="performed_identity_changes")
    group = models.ForeignKey(IdentityGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name="changes")
    membership = models.ForeignKey(IdentityMembership, on_delete=models.SET_NULL, null=True, blank=True, related_name="changes")
    action = models.CharField(max_length=20, choices=Action.choices)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["owner", "action", "-created_at"])]


class IdentityGroupExport(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    class ReportFormat(models.TextChoices):
        JSON = "json", "JSON"
        CSV = "csv", "CSV"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="identity_group_exports",
    )
    identity_group = models.ForeignKey(
        IdentityGroup,
        on_delete=models.CASCADE,
        related_name="exports",
    )
    include_originals = models.BooleanField(default=True)
    include_preprocessed = models.BooleanField(default=False)
    include_enhanced = models.BooleanField(default=True)
    include_report = models.BooleanField(default=True)
    report_format = models.CharField(
        max_length=8,
        choices=ReportFormat.choices,
        default=ReportFormat.JSON,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    export_file = models.FileField(upload_to=identity_export_upload_path, blank=True)
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
            models.Index(
                fields=["owner", "status", "-created_at"],
                name="identity_exp_owner_status_idx",
            ),
            models.Index(
                fields=["identity_group", "status"],
                name="identity_exp_group_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "owner", "identity_group", "include_originals",
                    "include_preprocessed", "include_enhanced",
                    "include_report", "report_format",
                ],
                condition=Q(status__in=["queued", "generating"]),
                name="identity_one_active_group_export",
            ),
        ]

    def clean(self):
        if self.identity_group_id and self.owner_id != self.identity_group.owner_id:
            raise ValidationError("Identity export ownership must match its group.")
        if not any((
            self.include_originals,
            self.include_preprocessed,
            self.include_enhanced,
            self.include_report,
        )):
            raise ValidationError("Select at least one export component.")

    def __str__(self):
        return f"{self.owner_id}:{self.identity_group_id}:{self.status}"


@receiver(post_delete, sender=IdentityGroupExport)
def delete_identity_export_file(sender, instance, **kwargs):
    if instance.export_file:
        instance.export_file.storage.delete(instance.export_file.name)

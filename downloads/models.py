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
        IDENTITY_GROUP_PACKAGE = "identity_group_package", "Identity group package"
        APPROVED_REQUEST_ZIP = "approved_request_zip", "Approved download request ZIP"

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


class DownloadRequest(models.Model):
    """A member's request to download several images of an organization that requires approval (``Organization.download_approval_required``).

    The images it names are its items and never change after submission (a different selection is a new request). The status follows from the items'
    decisions (``downloads.approval.recompute_status``): ``pending`` while no item is decided, ``in_review`` while some are and some are not, then
    ``approved`` / ``partially_approved`` / ``denied`` once every item is decided. ``version`` rises with every decision so two moderators cannot overwrite each other.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        PARTIALLY_APPROVED = "partially_approved", "Partially approved"
        DENIED = "denied", "Denied"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.PENDING, Status.IN_REVIEW)

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="download_requests")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="download_requests")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    requester_message = models.CharField(max_length=500, blank=True)
    # The caller's own token for "this submission": sending the same one again returns the request that already exists instead of making another.
    client_token = models.CharField(max_length=64, blank=True)
    version = models.PositiveIntegerField(default=0)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["organization", "status", "-created_at"]),
            models.Index(fields=["requester", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["requester", "client_token"], condition=~Q(client_token=""), name="downloads_request_unique_client_token"),
        ]

    def __str__(self):
        return f"download request {self.pk} ({self.status})"


class DownloadRequestItem(models.Model):
    """One image (and which of its files) of a download request, with the moderator's decision about it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"

    class ArtifactType(models.TextChoices):
        ENHANCED = "enhanced", "Enhanced image"      # the default: the approved enhanced result (``artifact_id`` is its result id)
        ORIGINAL = "original", "Original image"      # ``artifact_id`` is the image id; the standardized copy needs this one too
        EDITED = "edited", "Saved edit"              # ``artifact_id`` is the edit's id

    class DenialReason(models.TextChoices):
        PRIVACY = "privacy_or_consent", "Privacy or consent restriction"
        CLIENT = "client_restriction", "Client or event organizer restriction"
        RIGHTS = "distribution_rights", "Distribution rights unavailable"
        WRONG_SELECTION = "incorrect_selection", "Incorrect image selection"
        POLICY = "policy_restriction", "Policy restriction"
        NO_LONGER_ELIGIBLE = "no_longer_eligible", "The image is no longer eligible"
        OTHER = "other", "Other"

    request = models.ForeignKey(DownloadRequest, on_delete=models.CASCADE, related_name="items")
    image = models.ForeignKey("uploads.ImageUpload", on_delete=models.CASCADE, related_name="download_request_items")
    artifact_type = models.CharField(max_length=10, choices=ArtifactType.choices, default=ArtifactType.ENHANCED)
    artifact_id = models.PositiveBigIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    denial_reason_code = models.CharField(max_length=24, choices=DenialReason.choices, blank=True)
    denial_reason_text = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["request", "image", "artifact_type"], name="downloads_item_unique_image_artifact"),
        ]
        indexes = [models.Index(fields=["image", "status"])]

    def __str__(self):
        return f"item {self.pk} of request {self.request_id} ({self.status})"


class DownloadGrant(models.Model):
    """The permission one approved item gives: its requester may download this one file of this one image until ``expires_at``.

    It is valid only while it is not revoked, not expired, not used up, and the image is still accepted, still there, and its organization and the grantee's
    membership are still active (``downloads.approval.authorize_download`` checks all of it on every download, so rejecting an image or removing a member
    takes effect at once). Nothing here is a link: the file is only ever delivered by an endpoint that checks the grant.
    """

    item = models.OneToOneField(DownloadRequestItem, on_delete=models.CASCADE, related_name="grant")
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="download_grants")
    grantee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="download_grants")
    image = models.ForeignKey("uploads.ImageUpload", on_delete=models.CASCADE, related_name="download_grants")
    artifact_type = models.CharField(max_length=10, choices=DownloadRequestItem.ArtifactType.choices)
    artifact_id = models.PositiveBigIntegerField()
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    revoke_reason = models.CharField(max_length=500, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["grantee", "image", "artifact_type", "artifact_id"]),
            models.Index(fields=["organization", "expires_at"]),
        ]

    def __str__(self):
        return f"grant {self.pk} for image {self.image_id} ({self.artifact_type})"

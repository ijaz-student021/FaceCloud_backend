from django.conf import settings
from django.db import models


class ModeratorReview(models.Model):
    """One AI decision that landed in the ambiguous/uncertain band, queued for a human.

    Created by an additive hook next to the pipeline that produced the
    uncertain result (identities.services, quality_control.services,
    duplicates.services) -- never by rewriting that pipeline's own logic. See
    moderation/services.py's create_review_for_* functions.
    """

    class Source(models.TextChoices):
        QUALITY = "quality", "Quality"
        DUPLICATE = "duplicate", "Duplicate"
        IDENTITY = "identity", "Identity"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CORRECTED = "corrected", "Corrected"
        ESCALATED = "escalated", "Escalated"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="moderator_reviews",
    )
    image = models.ForeignKey(
        "uploads.ImageUpload", on_delete=models.CASCADE, related_name="moderator_reviews",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    ai_recommendation = models.CharField(max_length=32, blank=True)
    ai_confidence = models.FloatField(null=True, blank=True)
    # Exactly one of these is set, matching `source`. Kept as three specific
    # FKs rather than a generic relation so each is a real, queryable,
    # database-enforced link back to the record a decision writes into.
    quality_decision = models.ForeignKey(
        "quality_control.ImageQualityDecision", on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderator_reviews",
    )
    duplicate_match = models.ForeignKey(
        "duplicates.DuplicateMatch", on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderator_reviews",
    )
    identity_membership = models.ForeignKey(
        "identities.IdentityMembership", on_delete=models.CASCADE, null=True, blank=True,
        related_name="moderator_reviews",
    )
    assigned_moderator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_moderator_reviews",
    )
    decision_reason = models.TextField(blank=True)
    decision_notes = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["quality_decision"], condition=models.Q(quality_decision__isnull=False),
                name="moderation_unique_quality_review",
            ),
            models.UniqueConstraint(
                fields=["duplicate_match"], condition=models.Q(duplicate_match__isnull=False),
                name="moderation_unique_duplicate_review",
            ),
            models.UniqueConstraint(
                fields=["identity_membership"], condition=models.Q(identity_membership__isnull=False),
                name="moderation_unique_identity_review",
            ),
        ]

    def __str__(self):
        return f"{self.source} review {self.pk} for org {self.organization_id}"

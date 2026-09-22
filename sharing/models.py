from django.conf import settings
from django.db import models
from django.utils import timezone


class ShareLink(models.Model):
    """A revocable, expiring link that shows one result to someone without an account.

    Only the SHA-256 of the token is stored. The raw token is returned once, at
    creation, so a database leak does not reveal working links.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="share_links",
    )
    enhancement_result = models.ForeignKey(
        "enhancement.ImageEnhancementResult", on_delete=models.CASCADE,
        related_name="share_links",
    )
    # When set, the link shows this edit instead of the enhanced result itself.
    edit_version = models.ForeignKey(
        "enhancement.ImageEditVersion", on_delete=models.CASCADE,
        related_name="share_links", null=True, blank=True,
    )
    token_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=120, blank=True)
    watermark = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    access_count = models.PositiveIntegerField(default=0)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "-created_at"])]

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"

    def state(self, now=None):
        if self.revoked_at:
            return self.State.REVOKED
        if self.expires_at <= (now or timezone.now()):
            return self.State.EXPIRED
        return self.State.ACTIVE

    def __str__(self):
        return f"share {self.pk} of result {self.enhancement_result_id}"


class ShareLinkAccess(models.Model):
    """One view of a link. Deliberately stores no IP address or other identity."""

    link = models.ForeignKey(ShareLink, on_delete=models.CASCADE, related_name="accesses")
    accessed_at = models.DateTimeField(auto_now_add=True)
    user_agent = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-accessed_at"]
        indexes = [models.Index(fields=["link", "-accessed_at"])]

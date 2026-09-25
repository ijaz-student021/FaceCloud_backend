import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver


def collage_upload_path(instance, filename):
    return f"users/{instance.owner_id}/collages/{uuid.uuid4().hex}.jpg"


class Collage(models.Model):
    class SourceType(models.TextChoices):
        EVENT = "event", "Event"
        PERSON = "person", "Person"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="collages")
    source_type = models.CharField(max_length=8, choices=SourceType.choices)
    # The event id (first photo's id) or the identity-group id the collage was made from.
    source_ref = models.PositiveBigIntegerField()
    title = models.CharField(max_length=80, blank=True)
    image_ids = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    collage_file = models.FileField(upload_to=collage_upload_path, blank=True)
    # Crop rectangles and, for a person collage, the group's display name. It
    # never holds embeddings, similarity scores or any other biometric value.
    manifest = models.JSONField(default=dict, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    task_id = models.CharField(max_length=255, blank=True, db_index=True)
    failure_reason = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "status", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["owner"], condition=Q(status__in=["queued", "generating"]),
                name="collage_one_active_per_owner",
            ),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.source_type}:{self.source_ref}:{self.status}"


@receiver(post_delete, sender=Collage)
def delete_collage_file(sender, instance, **kwargs):
    if instance.collage_file:
        instance.collage_file.storage.delete(instance.collage_file.name)

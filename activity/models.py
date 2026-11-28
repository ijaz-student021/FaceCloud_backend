from django.conf import settings
from django.db import models


class ActivityEventLockManager(models.Manager):
    """An activity event names what it is about by ``object_type`` and a text ``object_id``, so the organization lock (organizations/access.py) is applied by looking
    those objects up: the timeline of a person locked out of an organization no longer mentions what they did with its content."""

    def get_queryset(self):
        from organizations.access import locked_org_ids

        queryset = super().get_queryset()
        ids = locked_org_ids()
        if not ids:
            return queryset
        from django.db.models.functions import Cast

        from enhancement.models import ImageEnhancementResult
        from identities.models import IdentityGroup, IdentityGroupExport
        from sharing.models import ShareLink
        from uploads.models import ImageUpload, UploadBatch

        def text_ids(model, **lookup):
            return model._base_manager.filter(**lookup).annotate(as_text=Cast("pk", models.CharField())).values("as_text")

        in_org = "batch__organization_id__in"
        image = "image__batch__organization_id__in"
        locked = (
            models.Q(object_type="ImageUpload", object_id__in=text_ids(ImageUpload, **{in_org: ids}))
            | models.Q(object_type="UploadBatch", object_id__in=text_ids(UploadBatch, organization_id__in=ids))
            | models.Q(object_type="IdentityGroup", object_id__in=text_ids(IdentityGroup, organization_id__in=ids))
            | models.Q(object_type="IdentityGroupExport", object_id__in=text_ids(IdentityGroupExport, identity_group__organization_id__in=ids))
            | models.Q(object_type="ImageEnhancementResult", object_id__in=text_ids(ImageEnhancementResult, **{image: ids}))
            | models.Q(
                object_type="ShareLink",
                object_id__in=ShareLink._base_manager.filter(
                    models.Q(enhancement_result__image__batch__organization_id__in=ids)
                    | models.Q(edit_version__enhancement_result__image__batch__organization_id__in=ids)
                    | models.Q(identity_export__identity_group__organization_id__in=ids)
                ).annotate(as_text=Cast("pk", models.CharField())).values("as_text"),
            )
        )
        return queryset.exclude(locked)


class ActivityEvent(models.Model):
    class Category(models.TextChoices):
        UPLOAD = "upload", "Upload"
        PROCESSING = "processing", "Processing"
        MODERATION = "moderation", "Moderation"
        DOWNLOAD = "download", "Download"
        GROUP = "group", "Identity group"
        SHARE = "share", "Share"
        ADMIN = "admin", "Administration"
        PRIVACY = "privacy", "Privacy"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_events",
    )
    category = models.CharField(max_length=16, choices=Category.choices)
    event = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ActivityEventLockManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["category", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.category}:{self.event} ({self.user_id})"

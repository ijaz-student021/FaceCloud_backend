from django.contrib import admin

from .models import FaceValidationResult


@admin.register(FaceValidationResult)
class FaceValidationResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner",
        "image",
        "decision",
        "face_count",
        "detector",
        "per_face_metrics_enabled",
        "updated_at",
    )
    list_filter = (
        "decision",
        "detector",
        "per_face_metrics_enabled",
    )
    search_fields = (
        "owner__username",
        "owner__email",
        "image__original_name",
    )
    readonly_fields = (
        "coordinate_mapping",
        "faces",
        "created_at",
        "updated_at",
    )

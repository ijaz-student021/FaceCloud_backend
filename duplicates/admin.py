from django.contrib import admin

from .models import DuplicateAnalysis, DuplicateMatch


@admin.register(DuplicateAnalysis)
class DuplicateAnalysisAdmin(admin.ModelAdmin):
    list_display = (
        "id", "image", "owner", "status", "decision", "match_count", "updated_at",
    )
    list_filter = ("status", "decision", "policy_version")
    search_fields = ("image__original_name", "owner__username", "owner__email")
    readonly_fields = ("perceptual_hash", "face_fingerprints", "task_id")


@admin.register(DuplicateMatch)
class DuplicateMatchAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source_image", "matched_image", "match_type",
        "similarity_score", "review_status", "created_at",
    )
    list_filter = ("match_type", "review_status")
    search_fields = (
        "source_image__original_name", "matched_image__original_name",
        "owner__username", "owner__email",
    )

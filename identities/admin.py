from django.contrib import admin

from .models import (IdentityAnalysis, IdentityChange, IdentityFace, IdentityGroup,
                     IdentityGroupExport, IdentityMembership)


@admin.register(IdentityAnalysis)
class IdentityAnalysisAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "image", "status", "face_count", "review_count", "updated_at")
    list_filter = ("status", "provider")
    readonly_fields = ("task_id", "failure_reason")


@admin.register(IdentityFace)
class IdentityFaceAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "image", "face_index", "provider", "embedding_dimension")
    exclude = ("protected_embedding",)


@admin.register(IdentityGroup)
class IdentityGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "display_name", "state", "member_count", "updated_at")
    exclude = ("protected_centroid",)


@admin.register(IdentityMembership)
class IdentityMembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "identity_group", "state", "similarity_score", "created_at")
    readonly_fields = ("evidence",)


@admin.register(IdentityChange)
class IdentityChangeAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "actor", "action", "group", "membership", "created_at")
    list_filter = ("action",)
    readonly_fields = ("owner", "actor", "group", "membership", "action", "before", "after", "reason", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IdentityGroupExport)
class IdentityGroupExportAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "identity_group", "status", "file_size", "expires_at",
        "created_at",
    )
    list_filter = ("status", "report_format")
    readonly_fields = (
        "owner", "identity_group", "status", "progress_percentage", "manifest",
        "file_size", "file_checksum_sha256", "task_id", "failure_reason",
        "generated_at", "expires_at", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

from django.contrib import admin

from .models import EnhancementBatchJob, ImageEditVersion, ImageEnhancementResult


class ImageEnhancementResultInline(admin.TabularInline):
    model = ImageEnhancementResult
    fields = ("image", "requested_level", "applied_level", "status", "output_name")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(EnhancementBatchJob)
class EnhancementBatchJobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "source_batch", "requested_level", "status",
        "total_images", "processed_images", "created_at",
    )
    list_filter = ("requested_level", "status", "created_at")
    search_fields = ("owner__username", "owner__email", "task_id")
    readonly_fields = (
        "owner", "source_batch", "requested_level", "status", "total_images",
        "processed_images", "successful_images", "failed_images", "task_id",
        "failure_reason", "started_at", "finished_at", "created_at", "updated_at",
    )
    inlines = [ImageEnhancementResultInline]


@admin.register(ImageEnhancementResult)
class ImageEnhancementResultAdmin(admin.ModelAdmin):
    list_display = (
        "id", "image", "owner", "requested_level", "applied_level", "status",
        "output_format", "processing_duration_ms", "created_at",
    )
    list_filter = ("requested_level", "applied_level", "status", "output_format", "created_at")
    search_fields = ("output_name", "image__original_name", "owner__username", "owner__email")
    readonly_fields = (
        "owner", "image", "preprocessing_result", "batch_job", "requested_level",
        "applied_level", "status", "enhanced_file", "output_name", "output_format",
        "output_width", "output_height", "output_file_size", "processing_duration_ms",
        "applied_parameters", "failure_reason", "started_at", "finished_at",
        "created_at", "updated_at",
    )

    def delete_queryset(self, request, queryset):
        for result in queryset:
            result.delete()


@admin.register(ImageEditVersion)
class ImageEditVersionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "enhancement_result", "mode", "status",
        "is_current", "output_format", "created_at",
    )
    list_filter = ("mode", "status", "is_current", "output_format", "created_at")
    search_fields = ("output_name", "owner__username", "owner__email")
    readonly_fields = (
        "owner", "enhancement_result", "mode", "requested_adjustments",
        "applied_adjustments", "status", "edited_file", "output_name",
        "output_format", "output_width", "output_height", "output_file_size",
        "processing_duration_ms", "task_id", "quality_metadata",
        "failure_reason", "is_current", "started_at", "finished_at",
        "created_at", "updated_at",
    )

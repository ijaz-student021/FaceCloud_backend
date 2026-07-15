from django.contrib import admin

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch


class ImageUploadInline(admin.TabularInline):
    model = ImageUpload
    fields = ("original_name", "status", "file_size", "width", "height", "rejection_reason")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "upload_type", "status", "processing_status", "total_files", "processed_files", "created_at")
    list_filter = ("upload_type", "status", "processing_status", "created_at")
    search_fields = ("owner__username", "owner__email", "original_archive_name", "display_name")
    readonly_fields = (
        "total_files", "accepted_files", "rejected_files", "total_size", "processing_status",
        "processed_files", "processing_failed_files", "processing_task_id", "processing_error",
        "processing_started_at", "processing_finished_at", "created_at", "updated_at",
    )
    inlines = [ImageUploadInline]

    def delete_model(self, request, obj):
        from .services import delete_batch
        delete_batch(obj)

    def delete_queryset(self, request, queryset):
        from .services import delete_batch
        for batch in queryset:
            delete_batch(batch)


@admin.register(ImageUpload)
class ImageUploadAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "owner", "status", "file_size", "width", "height", "created_at")
    list_filter = ("status", "mime_type", "created_at")
    search_fields = ("original_name", "owner__username", "owner__email", "checksum_sha256")
    readonly_fields = (
        "owner", "batch", "original_file", "original_name", "file_extension", "mime_type",
        "file_size", "width", "height", "checksum_sha256", "status", "rejection_reason",
        "created_at", "updated_at",
    )

    def delete_queryset(self, request, queryset):
        for image in queryset:
            image.delete()


@admin.register(ImagePreprocessingResult)
class ImagePreprocessingResultAdmin(admin.ModelAdmin):
    list_display = ("id", "image", "owner", "status", "original_format", "output_format", "output_name", "updated_at")
    list_filter = ("status", "original_format", "output_format", "created_at")
    search_fields = ("output_name", "image__original_name", "owner__username", "owner__email")
    readonly_fields = (
        "image", "owner", "status", "requested_format", "requested_name", "original_format",
        "output_format", "output_name", "processed_file", "original_width", "original_height",
        "output_width", "output_height", "original_file_size", "processed_file_size",
        "had_transparency", "processing_duration_ms", "failure_reason", "created_at", "updated_at",
    )

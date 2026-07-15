from django.contrib import admin

from .models import ImageUpload, UploadBatch


class ImageUploadInline(admin.TabularInline):
    model = ImageUpload
    fields = ("original_name", "status", "file_size", "width", "height", "rejection_reason")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "upload_type", "status", "total_files", "accepted_files", "rejected_files", "created_at")
    list_filter = ("upload_type", "status", "created_at")
    search_fields = ("owner__username", "owner__email", "original_archive_name")
    readonly_fields = ("total_files", "accepted_files", "rejected_files", "total_size", "created_at", "updated_at")
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

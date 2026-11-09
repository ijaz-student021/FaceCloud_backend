import logging

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .database_copy import content_type_of, read_stored
from .models import EnhancedImageBlob, EnhancementBatchJob, ImageEditVersion, ImageEnhancementResult

logger = logging.getLogger(__name__)
IMAGE_SOURCES = ("original", "enhanced")


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
    exclude = ("enhanced_file",)  # shown by stored_file as text; as a form field it would be an upload widget with a link to MEDIA_URL
    readonly_fields = (
        "enhanced_image", "original_image", "database_copy_status",
        "owner", "image", "preprocessing_result", "batch_job", "requested_level",
        "applied_level", "status", "stored_file", "output_name", "output_format",
        "output_width", "output_height", "output_file_size", "processing_duration_ms",
        "applied_parameters", "failure_reason", "started_at", "finished_at",
        "created_at", "updated_at",
    )

    def delete_queryset(self, request, queryset):
        for result in queryset:
            result.delete()

    # The stored path is shown as text: the default link to MEDIA_URL leads nowhere, photographs are not served from a public folder.
    @admin.display(description="Enhanced file (path under the media folder)")
    def stored_file(self, obj):
        return obj.enhanced_file.name or "-"

    @admin.display(description="Enhanced image")
    def enhanced_image(self, obj):
        return self._preview(obj, "enhanced")

    @admin.display(description="Original image")
    def original_image(self, obj):
        return self._preview(obj, "original")

    @admin.display(description="Copy in the database")
    def database_copy_status(self, obj):
        if obj.pk is None:
            return "-"
        copy = EnhancedImageBlob.objects.filter(result=obj).only("byte_size", "sha256", "content_type").first()
        if copy is None:
            return "No row for this result in enhancement_enhancedimageblob."
        return f"Yes: {copy.byte_size:,} bytes, {copy.content_type}, SHA-256 {copy.sha256[:16]}…"

    def _preview(self, obj, which):
        if obj.pk is None:
            return "-"
        if not settings.ADMIN_IMAGE_PREVIEW_ENABLED:
            return "Previews are switched off (ADMIN_IMAGE_PREVIEW_ENABLED)."
        if which == "enhanced":
            if not obj.enhanced_file:
                return f"No enhanced image (status: {obj.get_status_display()})."
            source = "the copy in the database" if EnhancedImageBlob.objects.filter(result=obj).exists() else "the stored file"
        else:
            if not obj.image.original_file:
                return "-"
            source = "the stored file"
        url = reverse(f"{self.admin_site.name}:enhancement_imageenhancementresult_image", args=[obj.pk, which])
        return format_html(
            '<img src="{}" alt="{} image" style="max-width:480px;max-height:480px;border:1px solid #ccc"><br><small>Shown from {}.</small>',
            url, which, source,
        )

    def get_urls(self):
        route = path(
            "<int:object_id>/image/<str:which>/", self.admin_site.admin_view(self.image_view),
            name="enhancement_imageenhancementresult_image",
        )
        return [route, *super().get_urls()]

    def image_view(self, request, object_id, which):
        """One image of a result for the admin page: staff with the view permission only, and only while ADMIN_IMAGE_PREVIEW_ENABLED is on."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        if not settings.ADMIN_IMAGE_PREVIEW_ENABLED or which not in IMAGE_SOURCES:
            raise Http404
        result = self.get_object(request, str(object_id))
        if result is None:
            raise Http404
        data, content_type, source = self._image_bytes(result, which)
        logger.info("Administrator %s opened the %s image of enhancement result %s (from the %s).", request.user.pk, which, result.pk, source)
        response = HttpResponse(data, content_type=content_type)  # admin_view already sends private, no-store
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response["X-Image-Source"] = source
        return response

    def _image_bytes(self, result, which):
        if which == "enhanced":
            row = EnhancedImageBlob.objects.filter(result=result).values_list("data", "content_type").first()
            if row:
                return bytes(row[0]), row[1], "database"  # PostgreSQL hands back a memoryview
            field = result.enhanced_file
        else:
            field = result.image.original_file
        if not field:
            raise Http404
        try:
            return read_stored(field), content_type_of(field.name), "file"
        except OSError:
            raise Http404


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

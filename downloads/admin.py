from django.contrib import admin

from .models import DownloadEvent, DownloadPackage


@admin.register(DownloadPackage)
class DownloadPackageAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "batch", "package_type", "report_format", "status",
        "file_size", "expires_at", "created_at",
    )
    list_filter = ("package_type", "report_format", "status", "created_at")
    search_fields = ("owner__username", "owner__email", "display_name")
    readonly_fields = (
        "manifest", "file_checksum_sha256", "task_id", "generated_at",
        "created_at", "updated_at",
    )


@admin.register(DownloadEvent)
class DownloadEventAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "resource_type", "resource_id", "filename",
        "file_size", "downloaded_at",
    )
    list_filter = ("resource_type", "downloaded_at")
    search_fields = ("owner__username", "owner__email", "filename")
    readonly_fields = ("downloaded_at",)

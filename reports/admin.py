from django.contrib import admin

from .models import ProcessingReport


@admin.register(ProcessingReport)
class ProcessingReportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner",
        "report_type",
        "format",
        "status",
        "source_id",
        "created_at",
        "generated_at",
    )
    list_filter = ("report_type", "format", "status")
    search_fields = ("owner__username", "owner__email")
    readonly_fields = (
        "payload",
        "file_checksum_sha256",
        "task_id",
        "created_at",
        "updated_at",
        "generated_at",
    )

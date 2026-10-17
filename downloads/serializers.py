from datetime import datetime

from rest_framework import serializers

from .enrichment import KIND_BY_RESOURCE
from .models import DownloadEvent, DownloadPackage


class DownloadPackageRequestSerializer(serializers.Serializer):
    package_type = serializers.ChoiceField(choices=DownloadPackage.PackageType.choices)
    report_format = serializers.ChoiceField(
        choices=DownloadPackage.ReportFormat.choices,
        default=DownloadPackage.ReportFormat.CSV,
    )


class DownloadPackageSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = DownloadPackage
        fields = (
            "id", "batch", "package_type", "report_format", "status",
            "display_name", "manifest", "file_size", "failure_reason",
            "download_url", "generated_at", "expires_at", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_download_url(self, obj) -> str | None:
        if obj.status != DownloadPackage.Status.READY or not obj.package_file:
            return None
        request = self.context.get("request")
        path = f"/api/downloads/packages/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path


class DownloadEventSerializer(serializers.ModelSerializer):
    """One history row. The last four fields are additive; the view supplies them for the whole page."""

    kind = serializers.SerializerMethodField(help_text="single (one photo), batch (a ZIP of several) or report.")
    expires_at = serializers.SerializerMethodField(help_text="When the file or its live share link ends; null if neither expires.")
    image_count = serializers.SerializerMethodField(help_text="Photos in a ZIP or batch report; null for a single photo.")
    has_active_link = serializers.SerializerMethodField(help_text="Whether a live share link exists. The link itself is never listed.")

    class Meta:
        model = DownloadEvent
        fields = (
            "id", "resource_type", "resource_id", "batch_id", "image_id",
            "filename", "file_size", "content_type", "downloaded_at",
            "kind", "expires_at", "image_count", "has_active_link",
        )
        read_only_fields = fields

    def _extra(self, obj, key):
        return self.context.get("download_extras", {}).get(obj.pk, {}).get(key)

    def get_kind(self, obj) -> str:
        return KIND_BY_RESOURCE.get(obj.resource_type, "single")

    def get_expires_at(self, obj) -> datetime | None:
        return self._extra(obj, "expires_at")

    def get_image_count(self, obj) -> int | None:
        return self._extra(obj, "image_count")

    def get_has_active_link(self, obj) -> bool:
        return bool(self._extra(obj, "has_active_link"))

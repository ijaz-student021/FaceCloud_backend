from rest_framework import serializers

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

    def get_download_url(self, obj):
        if obj.status != DownloadPackage.Status.READY or not obj.package_file:
            return None
        request = self.context.get("request")
        path = f"/api/downloads/packages/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path


class DownloadEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = DownloadEvent
        fields = (
            "id", "resource_type", "resource_id", "batch_id", "image_id",
            "filename", "file_size", "content_type", "downloaded_at",
        )
        read_only_fields = fields

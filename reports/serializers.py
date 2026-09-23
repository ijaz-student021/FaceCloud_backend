from rest_framework import serializers

from .models import ProcessingReport


class ProcessingReportRequestSerializer(serializers.Serializer):
    format = serializers.ChoiceField(
        choices=ProcessingReport.Format.choices,
        default=ProcessingReport.Format.JSON,
    )


class ProcessingReportSerializer(serializers.ModelSerializer):
    source_id = serializers.IntegerField(read_only=True)
    source_name = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ProcessingReport
        fields = (
            "id",
            "report_type",
            "format",
            "status",
            "source_id",
            "source_name",
            "schema_version",
            "payload",
            "content_type",
            "file_size",
            "failure_reason",
            "download_url",
            "generated_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_source_name(self, obj) -> str:
        if obj.image_id:
            return obj.image.original_name
        return (
            obj.batch.display_name
            or obj.batch.original_archive_name
            or f"Batch {obj.batch_id}"
        )

    def get_download_url(self, obj) -> str | None:
        if obj.status != ProcessingReport.Status.READY or not obj.report_file:
            return None
        request = self.context.get("request")
        path = f"/api/reports/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path

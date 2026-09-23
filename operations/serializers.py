from rest_framework import serializers

from uploads.models import UploadBatch


class AdminFailedBatchSerializer(serializers.ModelSerializer):
    """A failed/stuck batch with its owner, for administrator visibility."""

    owner_id = serializers.IntegerField(read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True)

    class Meta:
        model = UploadBatch
        fields = [
            "id", "owner_id", "owner_username", "upload_type", "display_name",
            "processing_status", "current_stage", "total_files", "accepted_files",
            "processed_files", "processing_failed_files", "processing_error",
            "retry_count", "processing_started_at", "processing_finished_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

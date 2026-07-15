from rest_framework import serializers

from .models import ImageUpload, UploadBatch


class ImageUploadSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = [
            "id", "batch", "original_name", "file_extension", "mime_type",
            "file_size", "width", "height", "checksum_sha256", "status",
            "rejection_reason", "download_url", "created_at",
        ]

    def get_download_url(self, obj):
        if obj.status != "uploaded" or not obj.original_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path


class UploadBatchSerializer(serializers.ModelSerializer):
    images = ImageUploadSerializer(many=True, read_only=True)

    class Meta:
        model = UploadBatch
        fields = [
            "id", "upload_type", "original_archive_name", "status", "total_files",
            "accepted_files", "rejected_files", "total_size", "created_at", "updated_at", "images",
        ]

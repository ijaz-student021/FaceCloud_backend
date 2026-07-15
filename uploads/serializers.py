from rest_framework import serializers

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch


class ImagePreprocessingResultSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImagePreprocessingResult
        fields = [
            "id", "image", "status", "requested_format", "requested_name",
            "original_format", "output_format", "output_name", "original_width",
            "original_height", "output_width", "output_height", "original_file_size",
            "processed_file_size", "had_transparency", "processing_duration_ms",
            "failure_reason", "download_url", "created_at", "updated_at",
        ]

    def get_download_url(self, obj):
        if obj.status != "completed" or not obj.processed_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/{obj.image_id}/preprocessing/download/"
        return request.build_absolute_uri(path) if request else path


class PreprocessImageRequestSerializer(serializers.Serializer):
    output_format = serializers.ChoiceField(choices=["auto", "png", "jpeg"], default="auto")
    output_name = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    force = serializers.BooleanField(required=False, default=False)


class ImageUploadSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    preprocessing = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = [
            "id", "batch", "original_name", "file_extension", "mime_type",
            "file_size", "width", "height", "checksum_sha256", "status",
            "rejection_reason", "download_url", "preprocessing", "created_at",
        ]

    def get_download_url(self, obj):
        if obj.status != "uploaded" or not obj.original_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path

    def get_preprocessing(self, obj):
        try:
            result = obj.preprocessing_result
        except ImagePreprocessingResult.DoesNotExist:
            return None
        return ImagePreprocessingResultSerializer(result, context=self.context).data


class UploadBatchSerializer(serializers.ModelSerializer):
    images = ImageUploadSerializer(many=True, read_only=True)

    class Meta:
        model = UploadBatch
        fields = [
            "id", "upload_type", "original_archive_name", "display_name", "status", "total_files",
            "accepted_files", "rejected_files", "total_size", "created_at", "updated_at", "images",
        ]
        read_only_fields = [
            "upload_type", "original_archive_name", "status", "total_files", "accepted_files",
            "rejected_files", "total_size", "created_at", "updated_at", "images",
        ]

    def validate_display_name(self, value):
        return " ".join(value.split()).strip()

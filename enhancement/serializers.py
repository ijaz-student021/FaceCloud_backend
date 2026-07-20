from rest_framework import serializers

from .models import EnhancementBatchJob, EnhancementLevel, ImageEnhancementResult


class EnhancementRequestSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=EnhancementLevel.choices, default=EnhancementLevel.AUTO)


class ImageEnhancementResultSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    original_download_url = serializers.SerializerMethodField()
    standardized_download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImageEnhancementResult
        fields = [
            "id", "image", "batch_job", "requested_level", "applied_level", "status",
            "output_name", "output_format", "output_width", "output_height",
            "output_file_size", "processing_duration_ms", "task_id", "applied_parameters",
            "failure_reason", "original_download_url", "standardized_download_url",
            "download_url", "started_at", "finished_at", "created_at",
        ]

    def _absolute(self, path):
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_original_download_url(self, obj):
        return self._absolute(f"/api/images/{obj.image_id}/download/")

    def get_standardized_download_url(self, obj):
        return self._absolute(f"/api/images/{obj.image_id}/preprocessing/download/")

    def get_download_url(self, obj):
        if obj.status != "completed" or not obj.enhanced_file:
            return None
        path = f"/api/enhancements/results/{obj.pk}/download/"
        return self._absolute(path)


class EnhancementBatchJobSerializer(serializers.ModelSerializer):
    progress_percentage = serializers.FloatField(read_only=True)
    results = ImageEnhancementResultSerializer(many=True, read_only=True)

    class Meta:
        model = EnhancementBatchJob
        fields = [
            "id", "source_batch", "requested_level", "status", "total_images",
            "processed_images", "successful_images", "failed_images",
            "progress_percentage", "failure_reason", "started_at", "finished_at",
            "created_at", "updated_at", "results",
        ]

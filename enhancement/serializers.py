from rest_framework import serializers

from .models import (
    EditMode,
    EnhancementBatchJob,
    EnhancementLevel,
    EnhancementStatus,
    EnhancementStyle,
    ImageEditVersion,
    ImageEnhancementResult,
)


class EnhancementRequestSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=EnhancementLevel.choices, default=EnhancementLevel.AUTO)
    style = serializers.ChoiceField(choices=EnhancementStyle.choices, default=EnhancementStyle.NATURAL)


class GalleryAdjustmentSerializer(serializers.Serializer):
    brightness = serializers.FloatField(min_value=-100, max_value=100, default=0)
    contrast = serializers.FloatField(min_value=-100, max_value=100, default=0)
    shadows = serializers.FloatField(min_value=-100, max_value=100, default=0)
    saturation = serializers.FloatField(min_value=-100, max_value=100, default=0)
    highlights = serializers.FloatField(min_value=-100, max_value=100, default=0)
    warmth = serializers.FloatField(min_value=-100, max_value=100, default=0)
    intensity = serializers.FloatField(min_value=0, max_value=100, default=100)


class GalleryEditRequestSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=EditMode.choices, default=EditMode.AUTO)
    adjustments = GalleryAdjustmentSerializer(required=False, default=dict)

    def validate(self, attrs):
        if attrs["mode"] == EditMode.AUTO:
            attrs["adjustments"] = {}
        return attrs


class ImageEditVersionSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImageEditVersion
        fields = [
            "id", "enhancement_result", "mode", "requested_adjustments",
            "applied_adjustments", "status", "output_name", "output_format",
            "output_width", "output_height", "output_file_size",
            "processing_duration_ms", "task_id", "quality_metadata",
            "failure_reason", "is_current", "download_url", "started_at",
            "finished_at", "created_at", "updated_at",
        ]

    def get_download_url(self, obj):
        if obj.status != EnhancementStatus.COMPLETED or not obj.edited_file:
            return None
        path = f"/api/enhancements/edits/{obj.pk}/download/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class ImageEnhancementResultSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    original_download_url = serializers.SerializerMethodField()
    standardized_download_url = serializers.SerializerMethodField()
    editor_url = serializers.SerializerMethodField()
    current_edit = serializers.SerializerMethodField()

    class Meta:
        model = ImageEnhancementResult
        fields = [
            "id", "image", "batch_job", "requested_level", "applied_level",
            "requested_style", "status",
            "output_name", "output_format", "output_width", "output_height",
            "output_file_size", "processing_duration_ms", "task_id", "applied_parameters",
            "failure_reason", "original_download_url", "standardized_download_url",
            "download_url", "editor_url", "current_edit", "started_at",
            "finished_at", "created_at",
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

    def get_editor_url(self, obj):
        if obj.status != EnhancementStatus.COMPLETED or not obj.enhanced_file:
            return None
        return self._absolute(f"/api/enhancements/results/{obj.pk}/edits/")

    def get_current_edit(self, obj):
        current = next(
            (edit for edit in obj.edit_versions.all() if edit.is_current), None
        )
        if current is None:
            return None
        return ImageEditVersionSerializer(
            current, context=self.context
        ).data


class ImageEnhancementComparisonSerializer(serializers.Serializer):
    """Public, owner-scoped comparison contract for a completed result."""

    METRICS = {
        "brightness": "mean_luminance",
        "contrast": "contrast_stddev",
        "sharpness": "laplacian_sharpness",
        "noise": "estimated_noise",
        "shadow_clipping": "shadow_clipping_percent",
        "highlight_clipping": "highlight_clipping_percent",
    }

    result_id = serializers.IntegerField(source="pk", read_only=True)
    image_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    requested_level = serializers.CharField(read_only=True)
    applied_level = serializers.CharField(read_only=True)
    algorithm = serializers.SerializerMethodField()
    processing_duration_ms = serializers.IntegerField(read_only=True)
    original = serializers.SerializerMethodField()
    standardized = serializers.SerializerMethodField()
    enhanced = serializers.SerializerMethodField()
    comparison = serializers.SerializerMethodField()
    warnings = serializers.SerializerMethodField()

    def _absolute(self, path):
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_algorithm(self, obj):
        return obj.applied_parameters.get("algorithm", "")

    def get_original(self, obj):
        image = obj.image
        return {
            "filename": image.original_name,
            "format": image.file_extension.lstrip(".").lower(),
            "width": image.width,
            "height": image.height,
            "file_size": image.file_size,
            "download_url": self._absolute(f"/api/images/{image.pk}/download/"),
        }

    def get_standardized(self, obj):
        result = obj.preprocessing_result
        return {
            "filename": result.output_name,
            "format": result.output_format,
            "width": result.output_width,
            "height": result.output_height,
            "file_size": result.processed_file_size,
            "processing_duration_ms": result.processing_duration_ms,
            "download_url": self._absolute(
                f"/api/images/{obj.image_id}/preprocessing/download/"
            ),
        }

    def get_enhanced(self, obj):
        return {
            "filename": obj.output_name,
            "format": obj.output_format,
            "width": obj.output_width,
            "height": obj.output_height,
            "file_size": obj.output_file_size,
            "download_url": self._absolute(
                f"/api/enhancements/results/{obj.pk}/download/"
            ),
        }

    def get_comparison(self, obj):
        quality = obj.applied_parameters.get("quality_comparison", {})
        before = quality.get("before", {})
        after = quality.get("after", {})
        deltas = quality.get("deltas", {})
        metrics = {}
        for public_name, stored_name in self.METRICS.items():
            metrics[public_name] = {
                "before": before.get(stored_name),
                "after": after.get(stored_name),
                "change": deltas.get(stored_name),
            }
        return {
            "method": quality.get("method", ""),
            "reference_dimensions": quality.get("reference_dimensions", []),
            "metrics": metrics,
        }

    def get_warnings(self, obj):
        warnings = obj.applied_parameters.get("warnings", [])
        if not isinstance(warnings, list):
            return []
        return [str(warning) for warning in warnings]


class EnhancementBatchJobSerializer(serializers.ModelSerializer):
    progress_percentage = serializers.FloatField(read_only=True)
    results = ImageEnhancementResultSerializer(many=True, read_only=True)

    class Meta:
        model = EnhancementBatchJob
        fields = [
            "id", "source_batch", "requested_level", "requested_style", "status", "total_images",
            "processed_images", "successful_images", "failed_images",
            "progress_percentage", "failure_reason", "started_at", "finished_at",
            "created_at", "updated_at", "results",
        ]

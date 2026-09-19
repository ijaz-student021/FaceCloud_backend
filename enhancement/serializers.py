from drf_spectacular.utils import extend_schema_field, inline_serializer
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

    def get_download_url(self, obj) -> str | None:
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

    def get_original_download_url(self, obj) -> str:
        return self._absolute(f"/api/images/{obj.image_id}/download/")

    def get_standardized_download_url(self, obj) -> str:
        return self._absolute(f"/api/images/{obj.image_id}/preprocessing/download/")

    def get_download_url(self, obj) -> str | None:
        if obj.status != "completed" or not obj.enhanced_file:
            return None
        path = f"/api/enhancements/results/{obj.pk}/download/"
        return self._absolute(path)

    def get_editor_url(self, obj) -> str | None:
        if obj.status != EnhancementStatus.COMPLETED or not obj.enhanced_file:
            return None
        return self._absolute(f"/api/enhancements/results/{obj.pk}/edits/")

    @extend_schema_field(ImageEditVersionSerializer(allow_null=True))
    def get_current_edit(self, obj):
        current = next(
            (edit for edit in obj.edit_versions.all() if edit.is_current), None
        )
        if current is None:
            return None
        return ImageEditVersionSerializer(
            current, context=self.context
        ).data


def _file_summary(name, extra=()):
    fields = {
        "filename": serializers.CharField(),
        "format": serializers.CharField(),
        "width": serializers.IntegerField(allow_null=True),
        "height": serializers.IntegerField(allow_null=True),
        "file_size": serializers.IntegerField(),
        "download_url": serializers.CharField(),
    }
    fields.update({key: serializers.IntegerField() for key in extra})
    return inline_serializer(name, fields)


ORIGINAL_FILE_SUMMARY = _file_summary("ComparisonOriginalFile")
STANDARDIZED_FILE_SUMMARY = _file_summary(
    "ComparisonStandardizedFile", extra=("processing_duration_ms",)
)
ENHANCED_FILE_SUMMARY = _file_summary("ComparisonEnhancedFile")


_METRIC_CHANGE = inline_serializer("ComparisonMetricChange", {
    "before": serializers.FloatField(allow_null=True),
    "after": serializers.FloatField(allow_null=True),
    "change": serializers.FloatField(allow_null=True),
})


QUALITY_COMPARISON = inline_serializer("ComparisonQuality", {
    "method": serializers.CharField(),
    "reference_dimensions": serializers.ListField(child=serializers.IntegerField()),
    "metrics": inline_serializer("ComparisonMetrics", {
        name: _METRIC_CHANGE
        for name in (
            "brightness", "contrast", "sharpness", "noise",
            "shadow_clipping", "highlight_clipping",
        )
    }),
})


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

    def get_algorithm(self, obj) -> str:
        return obj.applied_parameters.get("algorithm", "")

    @extend_schema_field(ORIGINAL_FILE_SUMMARY)
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

    @extend_schema_field(STANDARDIZED_FILE_SUMMARY)
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

    @extend_schema_field(ENHANCED_FILE_SUMMARY)
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

    @extend_schema_field(QUALITY_COMPARISON)
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

    def get_warnings(self, obj) -> list[str]:
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

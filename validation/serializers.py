from rest_framework import serializers

from .models import FaceValidationResult


class FaceValidationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = FaceValidationResult
        fields = [
            "id", "image", "decision", "face_count", "primary_confidence",
            "primary_confidence_type", "faces", "rejection_reason", "message",
            "detector", "detector_version", "analysis_source",
            "coordinate_mapping", "per_face_metrics_enabled",
            "full_resolution_width", "full_resolution_height",
            "metrics_version",
            "processing_duration_ms", "created_at", "updated_at",
        ]

from rest_framework import serializers

from .models import ImageQualityDecision


class ImageQualityDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageQualityDecision
        fields = [
            "id", "image", "face_validation", "status", "decision", "overall_score",
            "brightness_score", "sharpness_score", "contrast_score", "noise_score",
            "exposure_score", "resolution_score", "measurements", "defects",
            "rejection_reasons", "message", "policy_version", "task_id",
            "failure_reason", "processing_duration_ms", "started_at", "finished_at",
            "created_at", "updated_at",
        ]

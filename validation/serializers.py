from rest_framework import serializers

from .models import FaceValidationResult


class FaceValidationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = FaceValidationResult
        fields = [
            "id", "image", "decision", "face_count", "primary_confidence",
            "faces", "rejection_reason", "message", "detector",
            "processing_duration_ms", "created_at", "updated_at",
        ]

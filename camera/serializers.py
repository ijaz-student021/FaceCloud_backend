from rest_framework import serializers

from .models import CameraSession, CameraSnapshot

MAX_SETTINGS_KEYS = 20


class CameraSnapshotSerializer(serializers.ModelSerializer):
    image_id = serializers.IntegerField(source="image.pk")
    batch_id = serializers.IntegerField(source="image.batch_id")
    image_status = serializers.CharField(source="image.status")
    rejection_reason = serializers.CharField(source="image.rejection_reason")

    class Meta:
        model = CameraSnapshot
        fields = ["id", "image_id", "batch_id", "image_status", "rejection_reason", "created_at"]
        read_only_fields = fields


class CameraSessionSerializer(serializers.ModelSerializer):
    snapshot_count = serializers.IntegerField(source="snapshots.count", read_only=True)

    class Meta:
        model = CameraSession
        fields = ["id", "device_label", "settings", "started_at", "ended_at", "last_activity_at", "snapshot_count"]
        read_only_fields = ["id", "started_at", "ended_at", "last_activity_at", "snapshot_count"]

    def validate_settings(self, value):
        if not isinstance(value, dict) or len(value) > MAX_SETTINGS_KEYS:
            raise serializers.ValidationError(f"Use an object with at most {MAX_SETTINGS_KEYS} keys.")
        for key, item in value.items():
            if len(str(key)) > 40 or not isinstance(item, (str, int, float, bool, type(None))) or len(str(item)) > 100:
                raise serializers.ValidationError("Keys up to 40 characters; values plain and up to 100 characters.")
        return value


class CameraSessionDetailSerializer(CameraSessionSerializer):
    snapshots = CameraSnapshotSerializer(many=True, read_only=True)

    class Meta(CameraSessionSerializer.Meta):
        fields = CameraSessionSerializer.Meta.fields + ["snapshots"]

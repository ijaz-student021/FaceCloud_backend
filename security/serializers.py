from rest_framework import serializers

from .models import SecurityAlert


class SecurityAlertSerializer(serializers.ModelSerializer):
    acknowledged_by = serializers.SerializerMethodField()

    class Meta:
        model = SecurityAlert
        fields = [
            "id", "rule", "subject", "severity", "status", "event_count", "threshold", "window_minutes",
            "first_seen_at", "last_seen_at", "acknowledged_by", "acknowledged_at", "acknowledgement_note",
            "created_at",
        ]
        read_only_fields = fields

    def get_acknowledged_by(self, alert) -> dict | None:
        user = alert.acknowledged_by
        return {"id": user.pk, "username": user.username} if user else None


class SecurityAlertDetailSerializer(SecurityAlertSerializer):
    class Meta(SecurityAlertSerializer.Meta):
        fields = SecurityAlertSerializer.Meta.fields + ["evidence"]
        read_only_fields = fields


class AcknowledgeSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=2000, help_text="What was checked or done. Required.")


class SecurityRulesSerializer(serializers.Serializer):
    window_minutes = serializers.IntegerField()
    failed_login_threshold = serializers.IntegerField()
    authorization_failure_threshold = serializers.IntegerField()
    rate_limit_threshold = serializers.IntegerField()
    event_retention_days = serializers.IntegerField()

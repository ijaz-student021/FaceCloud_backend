from rest_framework import serializers

from .models import NotificationPreference, UserNotification


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = [
            "photo_accepted",
            "photo_rejected",
            "batch_upload_finished",
            "person_group_created",
            "email_notifications",
            "live_session_alerts",
            "moderator_review_updates",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class UserNotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = UserNotification
        fields = [
            "id", "event_type", "severity", "title", "message",
            "resource_type", "resource_id", "action_path", "metadata",
            "is_read", "read_at", "email_status", "email_sent_at", "created_at",
        ]
        read_only_fields = fields

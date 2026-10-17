from rest_framework import serializers


class TimelineEntrySerializer(serializers.Serializer):
    # What the dashboard draws: an icon (type), a colour (status) and a sentence (message).
    id = serializers.CharField(help_text="Stable per entry, e.g. activity-12 or auth-7.")
    type = serializers.ChoiceField(
        choices=["upload", "enhance", "validate", "download", "live", "group", "reject", "login", "email", "admin"],
    )
    message = serializers.CharField()
    timestamp = serializers.DateTimeField(help_text="Same instant as created_at.")
    status = serializers.ChoiceField(choices=["success", "warning", "error", "info"])
    # The stored record, kept as it always was.
    source = serializers.ChoiceField(choices=["activity", "auth"])
    category = serializers.CharField()
    event = serializers.CharField()
    object_type = serializers.CharField()
    object_id = serializers.CharField()
    identifier = serializers.CharField()
    ip_address = serializers.IPAddressField(allow_null=True)
    details = serializers.JSONField()
    created_at = serializers.DateTimeField()


class TimelinePageSerializer(serializers.Serializer):
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    has_next = serializers.BooleanField()
    results = TimelineEntrySerializer(many=True)

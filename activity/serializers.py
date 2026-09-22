from rest_framework import serializers


class TimelineEntrySerializer(serializers.Serializer):
    source = serializers.ChoiceField(choices=["activity", "auth"])
    category = serializers.CharField()
    event = serializers.CharField()
    object_type = serializers.CharField()
    object_id = serializers.CharField()
    identifier = serializers.CharField()
    ip_address = serializers.IPAddressField(allow_null=True)
    details = serializers.JSONField()
    created_at = serializers.DateTimeField()

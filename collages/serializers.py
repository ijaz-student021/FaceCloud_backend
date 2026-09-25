from rest_framework import serializers

from .models import Collage


class CollageRequestSerializer(serializers.Serializer):
    source_type = serializers.ChoiceField(choices=Collage.SourceType.choices)
    event_id = serializers.IntegerField(required=False, min_value=1)
    gap_hours = serializers.FloatField(required=False, min_value=0.25, max_value=168)
    group_id = serializers.IntegerField(required=False, min_value=1)
    title = serializers.CharField(required=False, allow_blank=True, max_length=80)

    def validate(self, attrs):
        if attrs["source_type"] == Collage.SourceType.EVENT:
            if "event_id" not in attrs:
                raise serializers.ValidationError({"event_id": "This field is required for an event collage."})
            attrs["source_ref"] = attrs["event_id"]
        else:
            if "group_id" not in attrs:
                raise serializers.ValidationError({"group_id": "This field is required for a person collage."})
            attrs["source_ref"] = attrs["group_id"]
        return attrs


class CollageSerializer(serializers.ModelSerializer):
    photo_count = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Collage
        fields = [
            "id", "source_type", "source_ref", "title", "status", "photo_count", "image_ids",
            "manifest", "file_size", "download_url", "failure_reason",
            "generated_at", "expires_at", "created_at",
        ]
        read_only_fields = fields

    def get_photo_count(self, obj) -> int:
        return len(obj.image_ids)

    def get_download_url(self, obj) -> str | None:
        if obj.status != Collage.Status.READY:
            return None
        path = f"/api/collages/{obj.pk}/download/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

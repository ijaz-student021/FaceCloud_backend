from rest_framework import serializers

from uploads.models import ImageUpload


def _download_url(request, image_id):
    path = f"/api/images/{image_id}/download/"
    return request.build_absolute_uri(path) if request else path


def _thumbnail_url(request, image_id):
    path = f"/api/images/{image_id}/thumbnail/"
    return request.build_absolute_uri(path) if request else path


class EventSerializer(serializers.Serializer):
    id = serializers.IntegerField(help_text="The id of the event's first photo; stable as photos are added.")
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    photo_count = serializers.IntegerField()
    cover_image_id = serializers.IntegerField()
    cover_download_url = serializers.SerializerMethodField()
    cover_thumbnail_url = serializers.SerializerMethodField()

    def get_cover_download_url(self, event) -> str:
        return _download_url(self.context.get("request"), event["cover_image_id"])

    def get_cover_thumbnail_url(self, event) -> str:
        return _thumbnail_url(self.context.get("request"), event["cover_image_id"])

    def to_representation(self, event):
        return super().to_representation({**event, "cover_image_id": event["id"]})


class EventImageSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = ["id", "original_name", "width", "height", "created_at", "download_url", "thumbnail_url"]

    def get_download_url(self, obj) -> str:
        return _download_url(self.context.get("request"), obj.pk)

    def get_thumbnail_url(self, obj) -> str:
        return _thumbnail_url(self.context.get("request"), obj.pk)

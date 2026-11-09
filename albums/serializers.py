from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Album

TITLE_MAX = 120


class AlbumSerializer(serializers.ModelSerializer):
    photo_count = serializers.IntegerField(read_only=True, help_text="Photographs in the album that are still stored (a photograph that was deleted is gone from it).")
    cover_image = serializers.SerializerMethodField(help_text="The id of the cover photograph: the one chosen, else the first photograph added; null for an empty album.")
    cover_thumbnail_url = serializers.SerializerMethodField(help_text="Its thumbnail address (fetch it with the Authorization header, like any thumbnail); null for an empty album.")

    class Meta:
        model = Album
        fields = ("id", "title", "description", "photo_count", "cover_image", "cover_thumbnail_url", "created_at", "updated_at")
        read_only_fields = ("id", "photo_count", "created_at", "updated_at")

    def _cover_id(self, album):
        chosen = getattr(album, "visible_cover_id", None)
        return chosen if chosen is not None else getattr(album, "first_image_id", None)

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_cover_image(self, album):
        return self._cover_id(album)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_cover_thumbnail_url(self, album):
        cover = self._cover_id(album)
        if cover is None:
            return None
        request = self.context.get("request")
        path = f"/api/images/{cover}/thumbnail/"
        return request.build_absolute_uri(path) if request else path

    def validate_title(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Give the album a title.")
        return value


class AlbumWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=TITLE_MAX)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    image_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=200, help_text="Photographs to put in the album (up to 200).")

    def validate_title(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Give the album a title.")
        return value


class AlbumPatchSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=TITLE_MAX, required=False)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_title(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Give the album a title.")
        return value


class ImageIdsSerializer(serializers.Serializer):
    image_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=200, allow_empty=False, help_text="Up to 200 photograph ids.")


class AddResultSerializer(serializers.Serializer):
    added_ids = serializers.ListField(child=serializers.IntegerField())
    already_in_album_ids = serializers.ListField(child=serializers.IntegerField())
    not_found_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Not yours, not a stored photograph, or does not exist: nothing was done for them.")
    photo_count = serializers.IntegerField()


class RemoveResultSerializer(serializers.Serializer):
    removed_ids = serializers.ListField(child=serializers.IntegerField())
    not_in_album_ids = serializers.ListField(child=serializers.IntegerField())
    photo_count = serializers.IntegerField()


class CoverRequestSerializer(serializers.Serializer):
    image_id = serializers.IntegerField(min_value=1)

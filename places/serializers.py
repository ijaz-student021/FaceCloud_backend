from rest_framework import serializers

from .models import Place


class PlaceBriefSerializer(serializers.ModelSerializer):
    """What a photograph row says about where it was taken: names only, never coordinates."""

    class Meta:
        model = Place
        fields = ("id", "name", "region", "country", "country_code")


class PlaceSerializer(serializers.ModelSerializer):
    photo_count = serializers.IntegerField(read_only=True, help_text="The caller's own photographs taken here (not in the trash).")
    cover_thumbnail_url = serializers.SerializerMethodField(help_text="Thumbnail of one of the caller's photographs from here (a favourite first, else the newest); null if none.")
    latitude = serializers.FloatField(read_only=True, help_text="The centre of the place from the gazetteer: not the position of any photograph.")
    longitude = serializers.FloatField(read_only=True, help_text="The centre of the place from the gazetteer: not the position of any photograph.")

    class Meta:
        model = Place
        fields = ("id", "name", "region", "country", "country_code", "photo_count", "cover_thumbnail_url", "latitude", "longitude")

    def get_cover_thumbnail_url(self, place) -> str | None:
        cover = getattr(place, "cover_image_id", None)
        if cover is None:
            return None
        request = self.context.get("request")
        path = f"/api/images/{cover}/thumbnail/"
        return request.build_absolute_uri(path) if request else path


class PlacesResponseSerializer(serializers.Serializer):
    attribution = serializers.CharField(help_text="Credit that must be shown wherever place names are shown (GeoNames, CC BY 4.0).")
    names_available = serializers.BooleanField(help_text="false while this server has no approved place-name file: photographs keep their coordinates but get no place.")
    places = PlaceSerializer(many=True)


class LocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    place = PlaceBriefSerializer(allow_null=True)


class LocationRemoveRequestSerializer(serializers.Serializer):
    image_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=200, allow_empty=False, help_text="Up to 200 photo ids.")


class LocationRemoveResultSerializer(serializers.Serializer):
    removed_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Photos whose location was taken off (they had one).")
    without_location_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Photos of yours that had no location.")
    not_found_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Ids that are not your stored photographs.")

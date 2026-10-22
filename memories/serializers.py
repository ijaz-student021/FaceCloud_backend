from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

PERSON_PREVIEW = 4


def thumbnail_path(request, image_id):
    path = f"/api/images/{image_id}/thumbnail/"
    return request.build_absolute_uri(path) if request else path


class MemoryCardSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="Stable for the same photographs; pass it to GET /api/memories/{id}/. otd:<year>, event:<first photograph id> or person:<folder id>:<YYYY-MM>.")
    kind = serializers.CharField(help_text="on_this_day, event or person.")
    title = serializers.CharField()
    subtitle = serializers.CharField()
    start = serializers.DateTimeField(allow_null=True)
    end = serializers.DateTimeField(allow_null=True)
    photo_count = serializers.IntegerField(help_text="All the photographs of the moment (the highlights are a few of them).")
    cover_thumbnail_url = serializers.CharField(allow_null=True)
    preview_thumbnail_urls = serializers.ListField(child=serializers.CharField())
    filter = serializers.DictField(child=serializers.CharField(), help_text="The query for GET /api/images/my/ that lists every photograph of the moment (taken_from/taken_to, or person).")

    def to_representation(self, memory):
        request = self.context.get("request")
        highlights = memory.highlights
        return {
            "id": memory.id, "kind": memory.kind, "title": memory.title, "subtitle": memory.subtitle,
            "start": serializers.DateTimeField().to_representation(memory.start) if memory.start else None,
            "end": serializers.DateTimeField().to_representation(memory.end) if memory.end else None,
            "photo_count": len(memory.rows),
            "cover_thumbnail_url": thumbnail_path(request, highlights[0]["id"]) if highlights else None,
            "preview_thumbnail_urls": [thumbnail_path(request, row["id"]) for row in highlights[:PERSON_PREVIEW]],
            "filter": {key: str(value) for key, value in memory.filter.items()},
        }

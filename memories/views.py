from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.serializers import MyImageSerializer
from uploads.views import my_images_queryset

from .serializers import MemoryCardSerializer
from .services import build_memories, memory_by_id

DEFAULT_LIMIT = 10
MAX_LIMIT = 30


class MemoriesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "memories"

    @extend_schema(
        operation_id="memories_list",
        parameters=[OpenApiParameter("limit", int, required=False, description=f"How many memories; default {DEFAULT_LIMIT}, at most {MAX_LIMIT}.")],
        responses={200: MemoryCardSerializer(many=True), 400: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        raw = request.query_params.get("limit", "")
        if raw and (not raw.isdigit() or int(raw) < 1):
            raise ValidationError({"limit": "Limit must be a positive whole number."})
        limit = min(int(raw), MAX_LIMIT) if raw else DEFAULT_LIMIT
        return Response(MemoryCardSerializer(build_memories(request.user, limit), many=True, context={"request": request}).data)


class MemoryDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "memories"

    @extend_schema(operation_id="memories_retrieve", responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT})
    def get(self, request, memory_id):
        """One memory: the card and its best photographs (up to 24, oldest first) as the same rows the gallery lists. All of its photographs: GET /api/images/my/ with the card's ``filter``."""
        memory = memory_by_id(request.user, memory_id)
        if memory is None:
            raise NotFound("That memory no longer exists.")
        order = {row["id"]: position for position, row in enumerate(sorted(memory.highlights, key=lambda row: (row["captured"], row["id"])))}
        photos = sorted(my_images_queryset(request.user).filter(pk__in=list(order)), key=lambda photo: order[photo.pk])
        card = MemoryCardSerializer(memory, context={"request": request}).data
        return Response({**card, "images": MyImageSerializer(photos, many=True, context={"request": request}).data})

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ActivityEvent
from .presentation import TYPES
from .serializers import TimelineEntrySerializer, TimelinePageSerializer
from .services import get_user_timeline

_CATEGORY_CHOICES = [choice for choice, _ in ActivityEvent.Category.choices] + ["auth"]


class UserActivityTimelineView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter("category", OpenApiTypes.STR, description=(
                "Filter to one category: " + ", ".join(_CATEGORY_CHOICES)
            )),
            OpenApiParameter("type", OpenApiTypes.STR, description=(
                "Filter to one kind of entry, exactly as each entry's `type` reads: " + ", ".join(TYPES)
            )),
            OpenApiParameter("page", OpenApiTypes.INT),
            OpenApiParameter("page_size", OpenApiTypes.INT),
        ],
        responses={200: TimelinePageSerializer},
    )
    def get(self, request):
        category = request.query_params.get("category")
        if category and category not in _CATEGORY_CHOICES:
            raise ValidationError({"category": "Choose a supported category."})

        kind = request.query_params.get("type")
        if kind and kind not in TYPES:
            raise ValidationError({"type": f"Choose one of: {', '.join(TYPES)}."})

        try:
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
        except ValueError as error:
            raise ValidationError({"page": "page and page_size must be integers."}) from error

        timeline = get_user_timeline(
            request.user, category=category, page=page, page_size=page_size, kind=kind
        )
        timeline["results"] = TimelineEntrySerializer(
            timeline["results"], many=True
        ).data
        return Response(timeline)

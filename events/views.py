from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import EventImageSerializer, EventSerializer
from .services import MAX_GAP_HOURS, MIN_GAP_HOURS, compute_events, get_event, uploaded_images

GAP_PARAMETER = OpenApiParameter(
    "gap_hours", float, required=False,
    description=(
        f"Hours of silence that start a new event ({MIN_GAP_HOURS}-{MAX_GAP_HOURS}). "
        "Use the same value for the list, detail and photos calls."
    ),
)


class EventPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 100


def parse_gap_hours(request):
    raw = request.query_params.get("gap_hours")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValidationError({"gap_hours": "Enter a number of hours."})
    if not MIN_GAP_HOURS <= value <= MAX_GAP_HOURS:
        raise ValidationError({"gap_hours": f"Must be between {MIN_GAP_HOURS} and {MAX_GAP_HOURS} hours."})
    return value


class EventListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(parameters=[GAP_PARAMETER], responses=EventSerializer(many=True))
    def get(self, request):
        events = compute_events(request.user, parse_gap_hours(request))
        paginator = EventPagination()
        page = paginator.paginate_queryset(events, request, view=self)
        return paginator.get_paginated_response(EventSerializer(page, many=True, context={"request": request}).data)


class EventDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(parameters=[GAP_PARAMETER], responses=EventSerializer)
    def get(self, request, event_id):
        event = get_event(request.user, event_id, parse_gap_hours(request))
        if event is None:
            raise Http404
        return Response(EventSerializer(event, context={"request": request}).data)


class EventPhotoListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EventImageSerializer
    pagination_class = EventPagination

    @extend_schema(parameters=[GAP_PARAMETER])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return uploaded_images(self.request.user).none()
        event = get_event(self.request.user, self.kwargs["event_id"], parse_gap_hours(self.request))
        if event is None:
            raise Http404
        return uploaded_images(self.request.user).filter(pk__in=event["image_ids"]).order_by("created_at", "id")

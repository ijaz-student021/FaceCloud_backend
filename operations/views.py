from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from .analytics import InvalidAnalyticsFilter, build_admin_analytics
from .jobs import queue_status
from .services import UNAVAILABLE, build_system_health


class SystemHealthView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT})
    def get(self, request):
        health = build_system_health()
        response_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if health["status"] == UNAVAILABLE
            else status.HTTP_200_OK
        )
        return Response(health, status=response_status)


class QueueStatusView(APIView):
    """Active and stale background work per job type, for administrators."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(queue_status())


class AdminAnalyticsView(APIView):
    """Platform-wide totals, rates and charts for administrators (Feature 32)."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    throttle_scope = "admin_analytics"

    @extend_schema(
        parameters=[
            OpenApiParameter("date_from", OpenApiTypes.DATETIME, description="ISO-8601; defaults to 30 days before date_to."),
            OpenApiParameter("date_to", OpenApiTypes.DATETIME, description="ISO-8601; defaults to now."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Filter enhancements by status."),
            OpenApiParameter("model", OpenApiTypes.STR, description="Filter enhancements by a model/algorithm name substring."),
            OpenApiParameter("owner", OpenApiTypes.INT, description="Restrict every total to one user's id."),
        ],
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        params = request.query_params
        try:
            payload = build_admin_analytics(
                date_from=params.get("date_from"), date_to=params.get("date_to"),
                status=params.get("status"), model=params.get("model"), owner=params.get("owner"),
            )
        except InvalidAnalyticsFilter as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response(payload)

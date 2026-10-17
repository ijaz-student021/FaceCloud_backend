from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole
from activity.services import record_activity_event

from .models import SecurityAlert
from .serializers import (
    AcknowledgeSerializer,
    SecurityAlertDetailSerializer,
    SecurityAlertSerializer,
    SecurityRulesSerializer,
)
from .services import acknowledge, evaluate_rules


class AlertPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class SecurityAlertListView(generics.ListAPIView):
    """Security alerts, newest activity first (administrators only).

    Rules are evaluated before the list is served, so it is current. An empty
    list means no rule crossed its threshold; it is not a statement that the
    system is secure.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    serializer_class = SecurityAlertSerializer
    pagination_class = AlertPagination

    @extend_schema(parameters=[
        OpenApiParameter("status", str, enum=SecurityAlert.Status.values),
        OpenApiParameter("rule", str, enum=SecurityAlert.Rule.values),
        OpenApiParameter("severity", str, enum=SecurityAlert.Severity.values),
    ])
    def get(self, request, *args, **kwargs):
        evaluate_rules()
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = SecurityAlert.objects.select_related("acknowledged_by").order_by("-last_seen_at", "-pk")
        for name, choices in (("status", SecurityAlert.Status.values), ("rule", SecurityAlert.Rule.values),
                              ("severity", SecurityAlert.Severity.values)):
            value = self.request.query_params.get(name)
            if value:
                if value not in choices:
                    raise ValidationError({name: f"Choose one of: {', '.join(choices)}."})
                queryset = queryset.filter(**{name: value})
        return queryset


class SecurityAlertDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={200: SecurityAlertDetailSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        return Response(SecurityAlertDetailSerializer(get_object_or_404(SecurityAlert, pk=pk)).data)


class SecurityAlertAcknowledgeView(APIView):
    """Mark an open alert handled. Its evidence and the underlying events are kept."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(request=AcknowledgeSerializer,
                   responses={200: SecurityAlertDetailSerializer, 400: OpenApiTypes.OBJECT,
                              404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request, pk):
        alert = get_object_or_404(SecurityAlert, pk=pk)
        payload = AcknowledgeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        alert, changed = acknowledge(alert, request.user, payload.validated_data["note"])
        if not changed:
            return Response({"detail": "This alert has already been acknowledged."}, status=status.HTTP_409_CONFLICT)
        record_activity_event(request.user, "admin", "security_alert_acknowledged", object_type="SecurityAlert",
                              object_id=alert.pk, details={"rule": alert.rule, "subject": alert.subject})
        return Response(SecurityAlertDetailSerializer(alert).data)


class SecurityRulesView(APIView):
    """The thresholds in force, for the Security page to state."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={200: SecurityRulesSerializer})
    def get(self, request):
        return Response(SecurityRulesSerializer({
            "window_minutes": settings.SECURITY_ALERT_WINDOW_MINUTES,
            "failed_login_threshold": settings.SECURITY_ALERT_FAILED_LOGIN_THRESHOLD,
            "authorization_failure_threshold": settings.SECURITY_ALERT_AUTHORIZATION_FAILURE_THRESHOLD,
            "rate_limit_threshold": settings.SECURITY_ALERT_RATE_LIMIT_THRESHOLD,
            "event_retention_days": settings.SECURITY_EVENT_RETENTION_DAYS,
        }).data)

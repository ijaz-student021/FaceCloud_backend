from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserNotification
from .serializers import NotificationPreferenceSerializer, UserNotificationSerializer
from .services import get_notification_preferences, mark_notification_read


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class NotificationListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserNotificationSerializer
    pagination_class = NotificationPagination

    def get_queryset(self):
        queryset = UserNotification.objects.filter(recipient=self.request.user)
        unread = self.request.query_params.get("unread")
        if unread is not None:
            if unread.lower() not in {"true", "false"}:
                raise ValidationError({"unread": "Use true or false."})
            queryset = queryset.filter(read_at__isnull=unread.lower() == "true")
        event_type = self.request.query_params.get("event_type")
        if event_type:
            if event_type not in UserNotification.EventType.values:
                raise ValidationError({"event_type": "Choose a supported event type."})
            queryset = queryset.filter(event_type=event_type)
        severity = self.request.query_params.get("severity")
        if severity:
            if severity not in UserNotification.Severity.values:
                raise ValidationError({"severity": "Choose a supported severity."})
            queryset = queryset.filter(severity=severity)
        return queryset


class NotificationDetailView(generics.RetrieveDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserNotificationSerializer

    def get_queryset(self):
        return UserNotification.objects.filter(recipient=self.request.user)


class NotificationUnreadCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        count = UserNotification.objects.filter(
            recipient=request.user, read_at__isnull=True
        ).count()
        return Response({"unread_count": count})


class NotificationMarkReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "notification_action"

    @extend_schema(request=None, responses={200: UserNotificationSerializer})
    def post(self, request, pk):
        notification = get_object_or_404(
            UserNotification.objects.filter(recipient=request.user), pk=pk
        )
        mark_notification_read(notification)
        return Response(UserNotificationSerializer(notification).data)


class NotificationMarkAllReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "notification_action"

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        updated = UserNotification.objects.filter(
            recipient=request.user, read_at__isnull=True
        ).update(read_at=timezone.now(), updated_at=timezone.now())
        return Response({"marked_read": updated}, status=status.HTTP_200_OK)


class NotificationPreferenceView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "notification_action"

    @extend_schema(responses={200: NotificationPreferenceSerializer})
    def get(self, request):
        preferences = get_notification_preferences(request.user)
        return Response(NotificationPreferenceSerializer(preferences).data)

    @extend_schema(
        request=NotificationPreferenceSerializer,
        responses={200: NotificationPreferenceSerializer},
    )
    def patch(self, request):
        preferences = get_notification_preferences(request.user)
        serializer = NotificationPreferenceSerializer(
            preferences, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

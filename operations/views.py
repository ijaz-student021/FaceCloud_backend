from django.conf import settings
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole
from uploads.models import BatchImageProgress, UploadBatch
from uploads.progress import batch_progress_payload
from uploads.services import BatchHasNoFailedImages, BatchRetryLimitReached, retry_batch

from .analytics import InvalidAnalyticsFilter, build_admin_analytics
from .audit import InvalidAuditFilter, build_audit_feed
from .jobs import InvalidJobKind, list_failed_jobs, queue_status
from .moderation_overview import InvalidModerationOverviewFilter, build_admin_moderation_overview
from .serializers import AdminAnalyticsSerializer, AdminFailedBatchDetailSerializer
from .services import UNAVAILABLE, build_system_health


class SystemHealthView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT})
    def get(self, request):
        health = build_system_health(use_cache=True)
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
        responses={200: AdminAnalyticsSerializer, 400: OpenApiTypes.OBJECT},
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
        return Response(AdminAnalyticsSerializer(payload).data)


class AdminListPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class FailedBatchListView(generics.ListAPIView):
    """Platform-wide failed/stuck upload batches, for administrators (Feature 33)."""

    serializer_class = AdminFailedBatchDetailSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    pagination_class = AdminListPagination

    def get_queryset(self):
        return (
            UploadBatch.objects.filter(processing_status="failed")
            .select_related("owner", "organization")
            .prefetch_related(Prefetch(
                "image_progress", queryset=BatchImageProgress.objects.filter(status="failed").order_by("image_id"), to_attr="failed_progress",
            ))
            .order_by("-updated_at")
        )


class AdminBatchRetryView(APIView):
    """Retry any user's failed batch (Feature 33). Reuses the owner-facing logic exactly."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    throttle_scope = "admin_batch_retry"

    @extend_schema(request=None, responses={202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request, batch_id):
        batch = get_object_or_404(UploadBatch, pk=batch_id)
        try:
            batch = retry_batch(batch)
        except (BatchHasNoFailedImages, BatchRetryLimitReached) as error:
            raise ValidationError({"detail": [str(error)]}) from error
        payload = batch_progress_payload(batch)
        # The new state under the same names the failed-batch list uses, so the row can be updated in place.
        payload.update(
            processing_status=batch.processing_status, max_retries=settings.BATCH_PROGRESS_MAX_RETRIES,
            retries_left=max(0, settings.BATCH_PROGRESS_MAX_RETRIES - batch.retry_count),
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class FailedJobListView(APIView):
    """The most recent failed job of every kind, across all owners (Feature 33)."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(
        parameters=[
            OpenApiParameter("kind", OpenApiTypes.STR, description="Restrict to one job kind (see /queue/ for the list)."),
            OpenApiParameter("owner", OpenApiTypes.INT, description="Restrict to one user's id."),
            OpenApiParameter("limit_per_kind", OpenApiTypes.INT, description="Most recent failures per kind (default 20, max 100)."),
        ],
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        params = request.query_params
        try:
            payload = list_failed_jobs(
                kind=params.get("kind"), owner=params.get("owner") or None,
                limit_per_kind=params.get("limit_per_kind", 20),
            )
        except (InvalidJobKind, ValueError) as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response(payload)


class AdminAuditView(APIView):
    """A combined, platform-wide feed of AuthEvent and ActivityEvent rows (Feature 33)."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    throttle_scope = "admin_audit"

    @extend_schema(
        parameters=[
            OpenApiParameter("user", OpenApiTypes.INT, description="Restrict to one user's id."),
            OpenApiParameter("category", OpenApiTypes.STR, description="'auth', or an activity category (upload, processing, moderation, ...)."),
            OpenApiParameter("source", OpenApiTypes.STR, description="'auth' or 'activity' only."),
            OpenApiParameter("q", OpenApiTypes.STR, description="Text to find in the actor's name, the event (its name or its sentence) or the category; up to 100 characters."),
            OpenApiParameter("page", OpenApiTypes.INT, description="1-based page number."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Default 50, max 200."),
        ],
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        params = request.query_params
        try:
            payload = build_audit_feed(
                user=params.get("user"), category=params.get("category"), source=params.get("source"),
                page=params.get("page", 1), page_size=params.get("page_size", 50), q=params.get("q"),
            )
        except InvalidAuditFilter as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response(payload)


class AdminModerationOverviewView(APIView):
    """Platform-wide moderation totals, organizations needing attention, and moderator workload (Feature 33)."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    throttle_scope = "admin_analytics"

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "stale_hours", OpenApiTypes.INT,
                description="How old a still-open review must be to count as stale (default 48).",
            ),
        ],
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        try:
            payload = build_admin_moderation_overview(stale_hours=request.query_params.get("stale_hours"))
        except InvalidModerationOverviewFilter as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response(payload)

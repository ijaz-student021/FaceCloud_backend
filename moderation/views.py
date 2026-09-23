from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from operations.serializers import AdminFailedBatchSerializer
from organizations.permissions import OrganizationScopedMixin
from uploads.models import UploadBatch
from uploads.services import BatchHasNoFailedImages, BatchRetryLimitReached

from .models import ModeratorReview
from .serializers import (
    ModeratorAssignSerializer,
    ModeratorDashboardSerializer,
    ModeratorDecisionSerializer,
    ModeratorEscalateSerializer,
    ModeratorReviewSerializer,
)
from .services import (
    assign_review,
    cancel_batch_for_organization,
    dashboard_summary,
    decide_review,
    escalate_review,
    retry_batch_for_organization,
)

ERROR_RESPONSES = {
    400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT,
    404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT,
}


class ModeratorReviewListView(OrganizationScopedMixin, generics.ListAPIView):
    require_owner_or_moderator = True
    serializer_class = ModeratorReviewSerializer

    def get_queryset(self):
        queryset = ModeratorReview.objects.filter(
            organization_id=self.kwargs["organization_id"],
        ).select_related("assigned_moderator", "decided_by")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        source_filter = self.request.query_params.get("source")
        if source_filter:
            queryset = queryset.filter(source=source_filter)
        return queryset


class ModeratorReviewDetailView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        return Response(ModeratorReviewSerializer(review).data)


class ModeratorReviewDecisionView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(
        request=ModeratorDecisionSerializer,
        responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES},
        summary="Approve, reject, correct, or hold a review",
    )
    def post(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        serializer = ModeratorDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decided = decide_review(
            review=review, actor=request.user, action=serializer.validated_data["action"],
            reason=serializer.validated_data.get("reason", ""),
            notes=serializer.validated_data.get("notes", ""),
            group_id=serializer.validated_data.get("group_id"), request=request,
        )
        return Response(ModeratorReviewSerializer(decided).data)


class ModeratorReviewEscalateView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(
        request=ModeratorEscalateSerializer,
        responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES},
        summary="Escalate a review",
    )
    def post(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        serializer = ModeratorEscalateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        escalated = escalate_review(
            review=review, actor=request.user,
            reason=serializer.validated_data.get("reason", ""), request=request,
        )
        return Response(ModeratorReviewSerializer(escalated).data)


class ModeratorReviewAssignView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(
        request=ModeratorAssignSerializer,
        responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES},
        summary="Assign a review to a specific moderator",
    )
    def post(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        serializer = ModeratorAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assigned = assign_review(
            review=review, actor=request.user, moderator=serializer.validated_data["moderator"],
            request=request,
        )
        return Response(ModeratorReviewSerializer(assigned).data)


class ModeratorDashboardView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(responses={200: ModeratorDashboardSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id):
        return Response(ModeratorDashboardSerializer(dashboard_summary(self.organization)).data)


class ModeratorBatchListView(OrganizationScopedMixin, generics.ListAPIView):
    require_owner_or_moderator = True
    serializer_class = AdminFailedBatchSerializer

    def get_queryset(self):
        queryset = (
            UploadBatch.objects.filter(organization_id=self.kwargs["organization_id"])
            .select_related("owner").order_by("-updated_at")
        )
        status_filter = self.request.query_params.get("processing_status")
        if status_filter:
            queryset = queryset.filter(processing_status=status_filter)
        return queryset


class ModeratorBatchRetryView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True
    throttle_scope = "org_batch_action"

    @extend_schema(
        request=None,
        responses={202: AdminFailedBatchSerializer, 400: OpenApiTypes.OBJECT, **ERROR_RESPONSES},
        summary="Retry a failed batch",
    )
    def post(self, request, organization_id, batch_id):
        batch = get_object_or_404(UploadBatch, pk=batch_id, organization_id=organization_id)
        try:
            batch = retry_batch_for_organization(batch=batch, actor=request.user, request=request)
        except (BatchHasNoFailedImages, BatchRetryLimitReached) as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response(AdminFailedBatchSerializer(batch).data, status=status.HTTP_202_ACCEPTED)


class ModeratorBatchCancelView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True
    throttle_scope = "org_batch_action"

    @extend_schema(
        request=None, responses={200: AdminFailedBatchSerializer, **ERROR_RESPONSES},
        summary="Cancel a batch still in progress",
    )
    def post(self, request, organization_id, batch_id):
        batch = get_object_or_404(UploadBatch, pk=batch_id, organization_id=organization_id)
        batch = cancel_batch_for_organization(batch=batch, actor=request.user, request=request)
        return Response(AdminFailedBatchSerializer(batch).data)

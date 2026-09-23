from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from identities.serializers import IdentityGroupSerializer
from operations.serializers import AdminFailedBatchSerializer
from organizations.permissions import OrganizationScopedMixin
from uploads.models import UploadBatch
from uploads.services import BatchHasNoFailedImages, BatchRetryLimitReached

from .models import ModeratorReport, ModeratorReview
from .serializers import (
    ModeratorAssignSerializer,
    ModeratorDashboardSerializer,
    ModeratorDecisionSerializer,
    ModeratorDuplicateGroupMergeSerializer,
    ModeratorEscalateSerializer,
    ModeratorReportCreateSerializer,
    ModeratorReportSerializer,
    ModeratorReviewSerializer,
)
from .services import (
    assign_review,
    cancel_batch_for_organization,
    dashboard_summary,
    decide_review,
    enqueue_moderator_report,
    escalate_review,
    expire_moderator_report_if_needed,
    merge_duplicate_identity_groups,
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


class ModeratorReportNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The report is not ready for download."
    default_code = "report_not_ready"


class ModeratorReportExpired(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "This generated report has expired."
    default_code = "report_expired"


class ModeratorReportListCreateView(OrganizationScopedMixin, generics.ListAPIView):
    """Org-scoped review-summary/moderator-activity reports (T-007 Phase F)."""

    require_owner_or_moderator = True
    serializer_class = ModeratorReportSerializer
    throttle_scope = "org_report"

    def get_queryset(self):
        return ModeratorReport.objects.filter(organization_id=self.kwargs["organization_id"])

    @extend_schema(
        request=ModeratorReportCreateSerializer,
        responses={201: ModeratorReportSerializer, 200: ModeratorReportSerializer, **ERROR_RESPONSES},
        summary="Request an organization report",
        description=(
            "Returns the existing in-flight report instead of creating a "
            "duplicate if one of the same type and format is already queued or generating."
        ),
    )
    def post(self, request, organization_id):
        serializer = ModeratorReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report, created = enqueue_moderator_report(
            organization=self.organization, requested_by=request.user,
            report_type=serializer.validated_data["report_type"],
            report_format=serializer.validated_data["format"],
        )
        return Response(
            ModeratorReportSerializer(report).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ModeratorReportDetailView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(responses={200: ModeratorReportSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id, report_id):
        report = get_object_or_404(ModeratorReport, pk=report_id, organization_id=organization_id)
        expire_moderator_report_if_needed(report)
        return Response(ModeratorReportSerializer(report).data)


class ModeratorReportFileView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(
        responses={200: OpenApiTypes.BINARY, 409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
        summary="Download a ready organization report",
    )
    def get(self, request, organization_id, report_id):
        report = get_object_or_404(ModeratorReport, pk=report_id, organization_id=organization_id)
        if expire_moderator_report_if_needed(report):
            raise ModeratorReportExpired()
        if report.status != ModeratorReport.Status.READY:
            raise ModeratorReportNotReady()
        if not report.report_file or not report.report_file.storage.exists(report.report_file.name):
            return Response(
                {"detail": "The generated report file is unavailable."},
                status=status.HTTP_410_GONE,
            )
        return FileResponse(
            report.report_file.open("rb"), as_attachment=True,
            filename=f"facecloud-{report.report_type}-report-{report.pk}.{report.format}",
            content_type=report.content_type or "application/octet-stream",
        )


class ModeratorDuplicateGroupMergeView(OrganizationScopedMixin, APIView):
    """Merges the identity groups behind a flagged duplicate (T-007 Phase G).

    Kept separate from ModeratorReviewDecisionView's "correct" action on
    purpose: confirming a duplicate match and merging the identity groups it
    points at are genuinely different operations that happen to originate
    from the same review.
    """

    require_owner_or_moderator = True

    @extend_schema(
        request=ModeratorDuplicateGroupMergeSerializer,
        responses={200: OpenApiTypes.OBJECT, **ERROR_RESPONSES},
        summary="Merge the identity groups behind a duplicate-match review",
    )
    def post(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        serializer = ModeratorDuplicateGroupMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source, target = merge_duplicate_identity_groups(
            review=review, actor=request.user,
            source_group_id=serializer.validated_data.get("source_group_id"),
            target_group_id=serializer.validated_data.get("target_group_id"),
            reason=serializer.validated_data.get("reason", ""), request=request,
        )
        return Response({
            "source": IdentityGroupSerializer(source).data,
            "target": IdentityGroupSerializer(target).data,
        })

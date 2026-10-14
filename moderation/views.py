from django.conf import settings
from django.db.models import F
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from identities.serializers import IdentityGroupSerializer
from operations.serializers import AdminFailedBatchSerializer
from organizations.models import OrganizationMembership
from organizations.permissions import OrganizationScopedMixin
from thumbnails.services import ThumbnailUnavailable, allowed_sizes, default_size, get_or_create_thumbnail
from uploads.models import UploadBatch
from uploads.services import BatchHasNoFailedImages, BatchRetryLimitReached

from .models import ModeratorReport, ModeratorReview
from .serializers import (
    ModeratorAssignSerializer,
    ModeratorBulkDecisionSerializer,
    ModeratorBulkResultSerializer,
    ModeratorDashboardSerializer,
    ModeratorDecisionSerializer,
    ModeratorDuplicateGroupMergeSerializer,
    ModeratorEscalateSerializer,
    ModeratorPerformanceSerializer,
    ModeratorReportCreateSerializer,
    ModeratorReportSerializer,
    ModeratorReviewSerializer,
)
from .services import (
    OPEN_REVIEW_STATUSES,
    assign_review,
    bulk_decide_reviews,
    cancel_batch_for_organization,
    claim_review,
    dashboard_summary,
    decide_review,
    enqueue_moderator_report,
    escalate_review,
    expire_moderator_report_if_needed,
    merge_duplicate_identity_groups,
    moderator_metrics,
    release_review_claim,
    retry_batch_for_organization,
)

ERROR_RESPONSES = {
    400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT,
    404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT,
}


def _is_true(value):
    return str(value).lower() in {"1", "true", "yes"}


@extend_schema(
    parameters=[
        OpenApiParameter("status", str, description="Filter by review status."),
        OpenApiParameter("source", str, description="Filter by what raised the review."),
        OpenApiParameter(
            "overdue", bool,
            description="Only reviews still undecided after their review deadline.",
        ),
        OpenApiParameter(
            "assignment", str,
            description=(
                "'mine' for reviews assigned to you, 'unassigned' for reviews nobody owns yet."
            ),
        ),
        OpenApiParameter("ordering", str, description="'due_at' to work to the deadline first."),
    ],
)
class ModeratorReviewListView(OrganizationScopedMixin, generics.ListAPIView):
    require_owner_or_moderator = True
    serializer_class = ModeratorReviewSerializer

    def get_queryset(self):
        queryset = ModeratorReview.objects.filter(
            organization_id=self.kwargs["organization_id"],
        ).select_related("assigned_moderator", "decided_by", "claimed_by", "image")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        source_filter = self.request.query_params.get("source")
        if source_filter:
            queryset = queryset.filter(source=source_filter)
        if _is_true(self.request.query_params.get("overdue")):
            queryset = queryset.filter(
                status__in=OPEN_REVIEW_STATUSES, decided_at__isnull=True,
                due_at__isnull=False, due_at__lt=timezone.now(),
            )
        assignment = self.request.query_params.get("assignment")
        if assignment == "mine":
            queryset = queryset.filter(assigned_moderator=self.request.user)
        elif assignment == "unassigned":
            queryset = queryset.filter(assigned_moderator__isnull=True)
        if self.request.query_params.get("ordering") == "due_at":
            # Nulls last: a review with no deadline must not jump the queue
            # ahead of one that has a real deadline today.
            queryset = queryset.order_by(F("due_at").asc(nulls_last=True), "created_at")
        return queryset


class ModeratorReviewDetailView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        return Response(ModeratorReviewSerializer(review).data)


class ModeratorReviewImageView(OrganizationScopedMixin, APIView):
    """The photo behind a review, as a thumbnail.

    A moderator cannot judge a photo they cannot see, so this exists -- but it
    is the narrowest access that makes the decision possible, not general
    access to the organization's images:

    * It is keyed by **review**, not by image. A moderator can only see photos
      they have actually been asked to judge; an image with no review in this
      organization has no URL here at all.
    * It serves **thumbnails only**, at the configured sizes. Judging blur,
      lighting, framing and whether a face is even present does not need the
      original, and not serving the original means a moderation role cannot be
      used to pull people's photos out of the system.
    * Organization scope and the owner/moderator role are enforced by
      OrganizationScopedMixin, exactly as for the review itself, so losing the
      role closes this off at the same moment it closes off the review.

    Views are not written to the audit log. A thumbnail is fetched every time
    the queue renders, so logging each one would bury the membership and
    decision history that log exists for; who worked a review is already
    recorded by its claim and its decision.
    """

    require_owner_or_moderator = True
    throttle_scope = "thumbnail"

    @extend_schema(
        parameters=[OpenApiParameter(
            "size", int, required=False,
            description="Longest edge in pixels. Must be one of the configured sizes (default 256, also 1024).",
        )],
        responses={200: OpenApiTypes.BINARY, **ERROR_RESPONSES},
        summary="The photo behind a review, as a thumbnail",
    )
    def get(self, request, organization_id, review_id):
        size = self._requested_size(request)
        review = get_object_or_404(
            ModeratorReview.objects.select_related("image"),
            pk=review_id, organization_id=organization_id,
        )
        try:
            thumbnail = get_or_create_thumbnail(review.image, size)
        except ThumbnailUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(thumbnail.file.open("rb"), content_type="image/jpeg")
        # A photo never changes after upload, so this browser can reuse it.
        response["Cache-Control"] = "private, max-age=3600"
        return response

    def _requested_size(self, request):
        raw = request.query_params.get("size", "")
        if raw == "":
            return default_size()
        if not raw.isdigit() or int(raw) not in allowed_sizes():
            raise ValidationError(
                {"size": f"Choose one of: {', '.join(str(size) for size in allowed_sizes())}."}
            )
        return int(raw)


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


class ModeratorReviewClaimView(OrganizationScopedMixin, APIView):
    """Take a review as the one you are working on now (a short soft lock)."""

    require_owner_or_moderator = True

    @extend_schema(
        request=None,
        responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES},
        summary="Claim a review",
        description=(
            "Holds the review for MODERATION_CLAIM_MINUTES so two moderators do "
            "not work the same photo. Answers 409 when another moderator holds "
            "it; claiming your own again extends the hold."
        ),
    )
    def post(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        claimed = claim_review(review=review, actor=request.user, request=request)
        return Response(ModeratorReviewSerializer(claimed).data)


class ModeratorReviewReleaseView(OrganizationScopedMixin, APIView):
    """Give a claimed review back to the queue."""

    require_owner_or_moderator = True

    @extend_schema(
        request=None,
        responses={200: ModeratorReviewSerializer, **ERROR_RESPONSES},
        summary="Release a claimed review",
        description=(
            "The moderator holding the review can release it; an organization "
            "owner can also release somebody else's claim."
        ),
    )
    def post(self, request, organization_id, review_id):
        review = get_object_or_404(ModeratorReview, pk=review_id, organization_id=organization_id)
        released = release_review_claim(
            review=review, actor=request.user,
            is_owner=self.active_membership.role == OrganizationMembership.Role.OWNER,
            request=request,
        )
        return Response(ModeratorReviewSerializer(released).data)


class ModeratorBulkDecisionView(OrganizationScopedMixin, APIView):
    """One decision across a selection of reviews, reporting each outcome."""

    require_owner_or_moderator = True
    throttle_scope = "org_bulk_moderation"

    @extend_schema(
        request=ModeratorBulkDecisionSerializer,
        responses={200: ModeratorBulkResultSerializer, **ERROR_RESPONSES},
        summary="Approve, reject or escalate many reviews at once",
        description=(
            "Each review is decided individually, so a row that is already "
            "decided or held by another moderator is reported under `failed` "
            "while the rest still go through. 'Correct' is not available in "
            "bulk because it needs a per-photo choice of person folder."
        ),
    )
    def post(self, request, organization_id):
        serializer = ModeratorBulkDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = bulk_decide_reviews(
            organization=self.organization, actor=request.user,
            review_ids=serializer.validated_data["review_ids"],
            action=serializer.validated_data["action"],
            reason=serializer.validated_data.get("reason", ""),
            notes=serializer.validated_data.get("notes", ""),
            request=request,
        )
        return Response(ModeratorBulkResultSerializer(result).data)


class ModeratorPerformanceView(OrganizationScopedMixin, APIView):
    """Throughput, speed and timeliness per moderator over a window."""

    require_owner_or_moderator = True
    throttle_scope = "org_moderation_metrics"

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "days", int,
                description="Window to measure, 1 to MODERATION_METRICS_MAX_DAYS. Defaults to 30.",
            ),
        ],
        responses={200: ModeratorPerformanceSerializer, **ERROR_RESPONSES},
        summary="Per-moderator performance",
    )
    def get(self, request, organization_id):
        days = self._window_days(request)
        return Response(ModeratorPerformanceSerializer(moderator_metrics(self.organization, days)).data)

    def _window_days(self, request):
        raw = request.query_params.get("days")
        if raw in (None, ""):
            return 30
        try:
            days = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"days": "Must be a whole number of days."}) from None
        if not 1 <= days <= settings.MODERATION_METRICS_MAX_DAYS:
            raise ValidationError({
                "days": f"Must be between 1 and {settings.MODERATION_METRICS_MAX_DAYS}.",
            })
        return days


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
            "Returns the existing in-flight report instead of creating a duplicate if one of the "
            "same type, format and date window is already queued or generating. The window "
            "(UTC, half-open) is stored on the report and applied by every report type; the "
            "payload states which timestamp each type filters on."
        ),
    )
    def post(self, request, organization_id):
        serializer = ModeratorReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report, created = enqueue_moderator_report(
            organization=self.organization, requested_by=request.user,
            report_type=serializer.validated_data["report_type"],
            report_format=serializer.validated_data["format"],
            date_from=serializer.validated_data["date_from"],
            date_to=serializer.validated_data["date_to"],
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


class ModeratorBatchImageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    original_name = serializers.CharField()
    upload_status = serializers.CharField()
    rejection_reason = serializers.CharField(allow_blank=True)
    stage = serializers.CharField(allow_null=True)
    progress_status = serializers.CharField(allow_null=True)
    progress_percentage = serializers.FloatField(allow_null=True)
    failure_stage = serializers.CharField(allow_blank=True, allow_null=True)
    failure_reason = serializers.CharField(allow_blank=True, allow_null=True)
    retry_count = serializers.IntegerField()
    quality_decision = serializers.CharField(allow_null=True)
    enhancement_status = serializers.CharField(allow_null=True)


class ModeratorBatchSummarySerializer(AdminFailedBatchSerializer):
    class Meta(AdminFailedBatchSerializer.Meta):
        fields = AdminFailedBatchSerializer.Meta.fields + ["rejected_files"]
        read_only_fields = fields


class ModeratorBatchDetailSerializer(serializers.Serializer):
    batch = ModeratorBatchSummarySerializer()
    images = ModeratorBatchImageSerializer(many=True)
    counts_from_images = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Per-image progress statuses counted from the rows, to reconcile with the batch counters.",
    )


class ModeratorBatchDetailView(OrganizationScopedMixin, APIView):
    """One organization batch with every image's outcome (owner or moderator).

    Metadata only: no image content. A batch of another organization is 404.
    """

    require_owner_or_moderator = True

    @extend_schema(responses={200: ModeratorBatchDetailSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id, batch_id):
        from collections import Counter

        from django.db.models import Prefetch

        from enhancement.models import ImageEnhancementResult
        from uploads.models import ImageUpload

        batch = get_object_or_404(
            UploadBatch.objects.select_related("owner"), pk=batch_id, organization_id=self.organization.pk,
        )
        images = list(
            ImageUpload.objects.filter(batch=batch)
            .select_related("processing_progress", "quality_decision")
            .prefetch_related(Prefetch(
                "enhancement_results", queryset=ImageEnhancementResult.objects.order_by("-created_at"),
                to_attr="latest_enhancements",
            ))
            .order_by("pk")
        )
        rows = []
        for image in images:
            progress = getattr(image, "processing_progress", None)
            decision = getattr(image, "quality_decision", None)
            enhancements = image.latest_enhancements
            rows.append({
                "id": image.pk, "original_name": image.original_name, "upload_status": image.status,
                "rejection_reason": image.rejection_reason or "",
                "stage": progress.stage if progress else None,
                "progress_status": progress.status if progress else None,
                "progress_percentage": progress.progress_percentage if progress else None,
                "failure_stage": progress.failure_stage if progress else None,
                "failure_reason": progress.failure_reason if progress else None,
                "retry_count": progress.retry_count if progress else 0,
                "quality_decision": (decision.decision or None) if decision else None,
                "enhancement_status": enhancements[0].status if enhancements else None,
            })
        counts = Counter(row["progress_status"] or "no_progress_record" for row in rows)
        return Response(ModeratorBatchDetailSerializer({
            "batch": batch, "images": rows, "counts_from_images": dict(counts),
        }).data)


def organization_images(organization):
    """Images submitted to ``organization``: its batches, never a personal space."""
    from uploads.models import ImageUpload

    if organization.is_personal:
        return ImageUpload.objects.none()
    return ImageUpload.objects.filter(batch__organization=organization)


class ModeratorImagePreviewView(OrganizationScopedMixin, APIView):
    """A thumbnail of an image submitted to this organization (owner or moderator).

    ``variant=original`` (default) is the uploaded image; ``variant=enhanced``
    is its latest completed enhancement output. Always a JPEG thumbnail of at
    most the requested size, never the full-size file. Images of another
    organization, and images in a personal space, are 404. Every preview is
    recorded in the organization's audit log.
    """

    require_owner_or_moderator = True
    throttle_scope = "thumbnail"

    @extend_schema(
        parameters=[
            OpenApiParameter("variant", str, enum=["original", "enhanced"], description="Default: original."),
            OpenApiParameter("size", int, description="Longest edge; one of THUMBNAIL_SIZES (default 256)."),
        ],
        responses={(200, "image/jpeg"): OpenApiTypes.BINARY, **ERROR_RESPONSES},
    )
    def get(self, request, organization_id, image_id):
        from django.http import FileResponse, HttpResponse
        from rest_framework.exceptions import NotFound, ValidationError

        from enhancement.models import EnhancementStatus
        from organizations.models import OrganizationAuditEvent
        from organizations.services import record_audit_event
        from thumbnails.services import (
            ThumbnailUnavailable, allowed_sizes, default_size, get_or_create_thumbnail, render_thumbnail,
        )

        variant = request.query_params.get("variant", "original")
        if variant not in ("original", "enhanced"):
            raise ValidationError({"variant": "Choose original or enhanced."})
        raw_size = request.query_params.get("size", "")
        if raw_size and (not raw_size.isdigit() or int(raw_size) not in allowed_sizes()):
            raise ValidationError({"size": f"Choose one of: {', '.join(str(size) for size in allowed_sizes())}."})
        size = int(raw_size) if raw_size else default_size()
        image = get_object_or_404(organization_images(self.organization).filter(status="uploaded"), pk=image_id)
        try:
            if variant == "original":
                thumbnail = get_or_create_thumbnail(image, size)
                response = FileResponse(thumbnail.file.open("rb"), content_type="image/jpeg")
            else:
                result = (image.enhancement_results.filter(status=EnhancementStatus.COMPLETED)
                          .exclude(enhanced_file="").order_by("-finished_at", "-pk").first())
                if result is None:
                    raise NotFound("This image has no completed enhancement.")
                payload, _width, _height = render_thumbnail(result.enhanced_file, size)
                response = HttpResponse(payload, content_type="image/jpeg")
        except ThumbnailUnavailable as error:
            raise NotFound(str(error)) from error
        record_audit_event(
            self.organization, request.user, OrganizationAuditEvent.Action.MODERATOR_IMAGE_PREVIEWED,
            target_type="image", target_id=image.pk, new_value={"variant": variant, "size": size},
            request=request,
        )
        response["Cache-Control"] = "private, no-store"
        return response


class ModeratorReprocessSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=["auto", "low", "medium", "high"], default="auto")
    style = serializers.ChoiceField(choices=["natural", "dynamic", "ai_max"], default="natural")
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class ModeratorReprocessResponseSerializer(serializers.Serializer):
    job_id = serializers.IntegerField()
    status = serializers.CharField()
    total_images = serializers.IntegerField()


class ModeratorBatchReprocessView(OrganizationScopedMixin, APIView):
    """Run enhancement again for a batch submitted to this organization.

    Every eligible image gets a new enhancement result (a new version) made
    from its preserved original. Originals and earlier outputs are never
    changed or removed. 409 when any image already has enhancement queued or
    running (nothing is started); the batch owner's fair-use limit applies
    (429). Recorded in the organization audit log.
    """

    require_owner_or_moderator = True
    throttle_scope = "org_batch_action"

    @extend_schema(request=ModeratorReprocessSerializer,
                   responses={201: ModeratorReprocessResponseSerializer, 409: OpenApiTypes.OBJECT,
                              429: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT, **ERROR_RESPONSES})
    def post(self, request, organization_id, batch_id):
        from enhancement.batch_jobs import start_batch_enhancement
        from enhancement.services import ensure_enhancement_capacity
        from organizations.models import OrganizationAuditEvent
        from organizations.services import record_audit_event

        if self.organization.is_personal:
            raise Http404("Batch not found.")
        batch = get_object_or_404(UploadBatch.objects.select_related("owner"), pk=batch_id,
                                  organization_id=self.organization.pk)
        payload = ModeratorReprocessSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ensure_enhancement_capacity(batch.owner, batch=True)
        job = start_batch_enhancement(batch, level=payload.validated_data["level"],
                                      style=payload.validated_data["style"])
        record_audit_event(
            self.organization, request.user, OrganizationAuditEvent.Action.BATCH_REPROCESSED_BY_MODERATOR,
            target_type="batch", target_id=batch.pk, reason=payload.validated_data["reason"],
            new_value={"job_id": job.pk, "level": job.requested_level, "style": job.requested_style,
                       "images": job.total_images},
            request=request,
        )
        return Response({"job_id": job.pk, "status": job.status, "total_images": job.total_images},
                        status=status.HTTP_201_CREATED)

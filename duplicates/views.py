from django.shortcuts import get_object_or_404
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload

from .models import DuplicateAnalysis, DuplicateMatch
from .serializers import (
    DuplicateAnalysisSerializer,
    DuplicateAnalyzeRequestSerializer,
    DuplicateMatchReviewSerializer,
    DuplicateMatchSerializer,
)
from .services import enqueue_duplicate_analysis, review_duplicate_match


class DuplicateImageAnalysisView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: DuplicateAnalysisSerializer})
    def get(self, request, image_id):
        analysis = get_object_or_404(
            DuplicateAnalysis.objects.filter(owner=request.user, image__trashed_at__isnull=True),
            image_id=image_id,
        )
        return Response(
            DuplicateAnalysisSerializer(analysis, context={"request": request}).data
        )

    @extend_schema(
        request=DuplicateAnalyzeRequestSerializer,
        responses={202: DuplicateAnalysisSerializer},
    )
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded").select_related(
                "preprocessing_result"
            ),
            pk=image_id,
        )
        payload = DuplicateAnalyzeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        analysis = enqueue_duplicate_analysis(
            image,
            force=payload.validated_data["force"],
        )
        return Response(
            DuplicateAnalysisSerializer(
                analysis,
                context={"request": request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class DuplicateAnalysisListView(generics.ListAPIView):
    serializer_class = DuplicateAnalysisSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = DuplicateAnalysis.objects.filter(owner=self.request.user, image__trashed_at__isnull=True)
        status_value = self.request.query_params.get("status")
        decision = self.request.query_params.get("decision")
        if status_value:
            if status_value not in DuplicateAnalysis.Status.values:
                raise ValidationError({"status": "Choose a valid analysis status."})
            queryset = queryset.filter(status=status_value)
        if decision:
            if decision not in DuplicateAnalysis.Decision.values:
                raise ValidationError({"decision": "Choose a valid duplicate decision."})
            queryset = queryset.filter(decision=decision)
        return queryset

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "request": self.request}


class DuplicateImageMatchListView(generics.ListAPIView):
    serializer_class = DuplicateMatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=self.request.user),
            pk=self.kwargs["image_id"],
        )
        return DuplicateMatch.objects.filter(
            owner=self.request.user, source_image__trashed_at__isnull=True, matched_image__trashed_at__isnull=True,
        ).filter(
            Q(source_image=image) | Q(matched_image=image)
        ).select_related("source_image", "matched_image", "analysis")


LARGEST_ROW_ID = 2**63 - 1                      # ids are 64-bit; a larger number cannot belong to any photograph


class DuplicateMatchListView(generics.ListAPIView):
    """Every duplicate match of the signed-in person: the pairs to decide on and the ones already decided (dismissed ones included), newest first.

    Paged by the project's default pagination (config/pagination.py), like the other lists: page and page_size, 20 by default, at most 100.
    """

    serializer_class = DuplicateMatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # A pair with a photograph in the trash is left out, exactly as the detail and review calls do.
        queryset = DuplicateMatch.objects.filter(
            owner=self.request.user, source_image__trashed_at__isnull=True, matched_image__trashed_at__isnull=True,
        ).select_related("source_image", "matched_image")
        review_status = self.request.query_params.get("review_status")
        if review_status:
            if review_status not in DuplicateMatch.ReviewStatus.values:
                raise ValidationError({"review_status": "Choose a valid review status: suspected, confirmed or not_duplicate."})
            queryset = queryset.filter(review_status=review_status)
        match_type = self.request.query_params.get("match_type")
        if match_type:
            if match_type not in DuplicateMatch.MatchType.values:
                raise ValidationError({"match_type": "Choose a valid match type: exact, perceptual or face_region."})
            queryset = queryset.filter(match_type=match_type)
        image = self.request.query_params.get("image", "").strip()
        if image:
            if not (image.isascii() and image.isdigit()) or int(image) == 0:
                raise ValidationError({"image": "Image must be a positive integer."})
            if int(image) > LARGEST_ROW_ID:
                return queryset.none()
            queryset = queryset.filter(Q(source_image_id=int(image)) | Q(matched_image_id=int(image)))
        return queryset.order_by("-created_at", "-id")

    @extend_schema(
        parameters=[
            OpenApiParameter("review_status", OpenApiTypes.STR, enum=list(DuplicateMatch.ReviewStatus.values), required=False, description="Only the matches with this decision."),
            OpenApiParameter("match_type", OpenApiTypes.STR, enum=list(DuplicateMatch.MatchType.values), required=False, description="Only this kind of match."),
            OpenApiParameter("image", OpenApiTypes.INT, required=False, description="Only the matches that have this photograph on either side."),
        ],
        responses={200: DuplicateMatchSerializer(many=True), 400: OpenApiTypes.OBJECT},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class DuplicateMatchDetailView(generics.RetrieveAPIView):
    serializer_class = DuplicateMatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return DuplicateMatch.objects.filter(owner=self.request.user, source_image__trashed_at__isnull=True, matched_image__trashed_at__isnull=True).select_related(
            "source_image", "matched_image", "analysis"
        )


class DuplicateMatchReviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=DuplicateMatchReviewSerializer,
        responses={200: DuplicateMatchSerializer},
    )
    def patch(self, request, pk):
        match = get_object_or_404(
            DuplicateMatch.objects.filter(owner=request.user, source_image__trashed_at__isnull=True, matched_image__trashed_at__isnull=True).select_related("analysis"),
            pk=pk,
        )
        payload = DuplicateMatchReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        match = review_duplicate_match(
            match,
            request.user,
            payload.validated_data["review_status"],
        )
        return Response(
            DuplicateMatchSerializer(match, context={"request": request}).data
        )

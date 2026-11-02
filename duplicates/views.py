from django.shortcuts import get_object_or_404
from django.db.models import Q
from drf_spectacular.utils import extend_schema
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

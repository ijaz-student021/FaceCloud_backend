from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload

from .models import ImageQualityDecision
from .serializers import ImageQualityDecisionSerializer
from .services import enqueue_quality_analysis


class ImageQualityView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: ImageQualityDecisionSerializer})
    def get(self, request, image_id):
        result = get_object_or_404(
            ImageQualityDecision.objects.filter(owner=request.user, image__trashed_at__isnull=True), image_id=image_id,
        )
        return Response(ImageQualityDecisionSerializer(result).data)

    @extend_schema(request=None, responses={202: ImageQualityDecisionSerializer})
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded"), pk=image_id,
        )
        result = enqueue_quality_analysis(image)
        return Response(ImageQualityDecisionSerializer(result).data, status=status.HTTP_202_ACCEPTED)


class QualityResultListView(generics.ListAPIView):
    serializer_class = ImageQualityDecisionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = ImageQualityDecision.objects.filter(owner=self.request.user, image__trashed_at__isnull=True)
        decision = self.request.query_params.get("decision")
        if decision:
            if decision not in ImageQualityDecision.Decision.values:
                raise ValidationError({"decision": "Use accepted, rejected, or needs_review."})
            queryset = queryset.filter(decision=decision)
        for parameter, lookup in (
            ("min_score", "overall_score__gte"),
            ("max_score", "overall_score__lte"),
        ):
            raw_value = self.request.query_params.get(parameter)
            if raw_value:
                try:
                    value = float(raw_value)
                except ValueError as error:
                    raise ValidationError({parameter: "Enter a number from 0 to 100."}) from error
                if not 0 <= value <= 100:
                    raise ValidationError({parameter: "Enter a number from 0 to 100."})
                queryset = queryset.filter(**{lookup: value})
        return queryset

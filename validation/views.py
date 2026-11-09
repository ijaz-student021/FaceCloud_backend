from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload

from .models import FaceValidationResult
from .serializers import FaceValidationResultSerializer
from .services import validate_image_faces


class ImageFaceValidationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: FaceValidationResultSerializer})
    def get(self, request, image_id):
        result = get_object_or_404(
            FaceValidationResult.objects.filter(owner=request.user, image__trashed_at__isnull=True), image_id=image_id,
        )
        return Response(FaceValidationResultSerializer(result).data)

    @extend_schema(
        request=None,
        responses={200: FaceValidationResultSerializer, 201: FaceValidationResultSerializer},
    )
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded"), pk=image_id,
        )
        created = not FaceValidationResult.objects.filter(image=image).exists()
        result = validate_image_faces(image)
        return Response(
            FaceValidationResultSerializer(result).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

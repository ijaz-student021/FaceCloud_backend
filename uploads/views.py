from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .serializers import (
    ImagePreprocessingResultSerializer,
    ImageUploadSerializer,
    PreprocessImageRequestSerializer,
    UploadBatchSerializer,
)
from .services import create_image_batch, create_zip_batch, delete_batch, preprocess_image


class SingleImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    def post(self, request):
        uploaded_file = request.FILES.get("file") or request.FILES.get("image")
        batch = create_image_batch(request.user, "single", [uploaded_file] if uploaded_file else [])
        return Response(UploadBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MultipleImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    def post(self, request):
        files = request.FILES.getlist("files")
        batch = create_image_batch(request.user, "multiple", files)
        return Response(UploadBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ZipImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "zip_upload"

    def post(self, request):
        uploaded_zip = request.FILES.get("file") or request.FILES.get("archive")
        if not uploaded_zip:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"file": "A ZIP archive is required."})
        batch = create_zip_batch(request.user, uploaded_zip)
        return Response(UploadBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class UploadBatchListView(generics.ListAPIView):
    serializer_class = UploadBatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UploadBatch.objects.filter(owner=self.request.user).prefetch_related("images")


class UploadBatchDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = UploadBatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UploadBatch.objects.filter(owner=self.request.user).prefetch_related("images")

    def perform_destroy(self, instance):
        delete_batch(instance)


class ImageUploadListView(generics.ListAPIView):
    serializer_class = ImageUploadSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ImageUpload.objects.filter(owner=self.request.user).select_related("batch")


class ImageUploadDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = ImageUploadSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ImageUpload.objects.filter(owner=self.request.user).select_related("batch")


class ImageDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded"),
            pk=pk,
        )
        if not image.original_file:
            return Response({"detail": "The stored image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        return FileResponse(
            image.original_file.open("rb"),
            as_attachment=True,
            filename=image.original_name,
            content_type=image.mime_type,
        )


class ImagePreprocessView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_preprocess"

    def post(self, request, pk):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded"),
            pk=pk,
        )
        request_serializer = PreprocessImageRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        result, processed = preprocess_image(
            image,
            requested_format=request_serializer.validated_data["output_format"],
            requested_name=request_serializer.validated_data["output_name"],
            force=request_serializer.validated_data["force"],
        )
        response_status = status.HTTP_201_CREATED if processed else status.HTTP_200_OK
        return Response(
            ImagePreprocessingResultSerializer(result, context={"request": request}).data,
            status=response_status,
        )


class ImagePreprocessingDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        result = get_object_or_404(
            ImagePreprocessingResult.objects.filter(owner=request.user).select_related("image"),
            image_id=pk,
        )
        return Response(ImagePreprocessingResultSerializer(result, context={"request": request}).data)


class ImagePreprocessingDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        result = get_object_or_404(
            ImagePreprocessingResult.objects.filter(owner=request.user, status="completed"),
            image_id=pk,
        )
        if not result.processed_file:
            return Response({"detail": "The processed image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        content_type = "image/png" if result.output_format == "png" else "image/jpeg"
        return FileResponse(
            result.processed_file.open("rb"),
            as_attachment=True,
            filename=result.output_name,
            content_type=content_type,
        )

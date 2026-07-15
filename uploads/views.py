from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ImageUpload, UploadBatch
from .serializers import ImageUploadSerializer, UploadBatchSerializer
from .services import create_image_batch, create_zip_batch, delete_batch


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


class UploadBatchDetailView(generics.RetrieveDestroyAPIView):
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

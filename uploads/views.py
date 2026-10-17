from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.db.models import OuterRef, Prefetch, Q, Subquery
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

from enhancement.models import ImageEnhancementResult
from identities.models import IdentityFace, IdentityMembership

from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .progress import batch_progress_payload
from .serializers import (
    ImagePreprocessingResultSerializer,
    ImageUploadSerializer,
    MyImageSerializer,
    PreprocessImageRequestSerializer,
    UploadBatchSerializer,
)
from .services import (
    cancel_batch,
    create_image_batch,
    create_zip_batch,
    delete_batch,
    enqueue_batch_preprocessing,
    preprocess_image,
    retry_batch,
)


def _all_request_files(request):
    return [uploaded for field in request.FILES for uploaded in request.FILES.getlist(field)]


class SingleImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "file": {"type": "string", "format": "binary"},
        }}},
        responses={201: UploadBatchSerializer},
    )
    def post(self, request):
        files = _all_request_files(request)
        if len(files) != 1:
            raise ValidationError({"file": "Select exactly one image for single-image upload."})
        batch = create_image_batch(request.user, "single", files)
        return Response(UploadBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MultipleImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
        }}},
        responses={201: UploadBatchSerializer},
    )
    def post(self, request):
        files = request.FILES.getlist("files")
        if len(files) < 2:
            raise ValidationError({"files": "Select at least two images for multiple-image upload."})
        if len(files) != len(_all_request_files(request)):
            raise ValidationError({"files": "Submit all images using the repeated 'files' field."})
        batch = create_image_batch(request.user, "multiple", files)
        batch = enqueue_batch_preprocessing(batch)
        return Response(UploadBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ZipImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "zip_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "file": {"type": "string", "format": "binary"},
        }}},
        responses={201: UploadBatchSerializer},
    )
    def post(self, request):
        files = _all_request_files(request)
        if len(files) != 1:
            raise ValidationError({"file": "Select exactly one ZIP archive."})
        uploaded_zip = files[0]
        batch = create_zip_batch(request.user, uploaded_zip)
        batch = enqueue_batch_preprocessing(batch)
        return Response(UploadBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class UploadBatchListView(generics.ListAPIView):
    serializer_class = UploadBatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        images = ImageUpload.objects.select_related("preprocessing_result")
        return UploadBatch.objects.filter(owner=self.request.user).prefetch_related(
            Prefetch("images", queryset=images)
        )


class UploadBatchDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = UploadBatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        images = ImageUpload.objects.select_related("preprocessing_result")
        return UploadBatch.objects.filter(owner=self.request.user).prefetch_related(
            Prefetch("images", queryset=images)
        )

    def perform_destroy(self, instance):
        delete_batch(instance)


class UploadBatchProgressView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request, batch_id):
        batch = get_object_or_404(UploadBatch.objects.filter(owner=request.user), pk=batch_id)
        return Response(batch_progress_payload(batch))


class UploadBatchCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    def post(self, request, batch_id):
        batch = get_object_or_404(UploadBatch.objects.filter(owner=request.user), pk=batch_id)
        batch = cancel_batch(batch)
        return Response(batch_progress_payload(batch))


class UploadBatchRetryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request, batch_id):
        batch = get_object_or_404(UploadBatch.objects.filter(owner=request.user), pk=batch_id)
        batch = retry_batch(batch)
        return Response(batch_progress_payload(batch), status=status.HTTP_202_ACCEPTED)


BATCH_FILTER = OpenApiParameter(
    "batch", int, required=False, description="Only photos from this upload batch (a batch id of yours).",
)


@extend_schema_view(get=extend_schema(parameters=[BATCH_FILTER]))
class ImageUploadListView(generics.ListAPIView):
    """Every photo the caller uploaded; ``?batch=<id>`` narrows it to one upload."""

    serializer_class = ImageUploadSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = ImageUpload.objects.filter(owner=self.request.user).select_related("batch", "preprocessing_result")
        batch = self.request.query_params.get("batch")
        if batch:
            if not batch.isdigit():
                raise ValidationError({"batch": "Batch must be a positive integer."})
            queryset = queryset.filter(batch_id=int(batch))
        return queryset


class MyImagesPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 100


ACTIVE_IDENTITY_STATES = ("auto_matched", "new_group", "confirmed")


def my_images_queryset(user):
    """One image row with everything MyImageSerializer reports, without N+1.

    Shared by the gallery and the single-image view so the two cannot drift
    apart again: the detail view used to answer with a thinner serializer, so a
    photo's own page could not say whether it had been accepted -- the one
    thing the person who uploaded it most wants to know.
    """
    latest = ImageEnhancementResult.objects.filter(image_id=OuterRef("pk")).order_by("-created_at")
    enhancement_results = ImageEnhancementResult.objects.order_by("-created_at")
    return (
        ImageUpload.objects.filter(owner=user)
        .select_related(
            "batch", "preprocessing_result", "face_validation",
            "quality_decision", "duplicate_analysis",
            "identity_analysis",
        )
        .prefetch_related(
            Prefetch(
                "enhancement_results",
                queryset=enhancement_results.prefetch_related("edit_versions"),
                to_attr="ordered_enhancement_results",
            ),
            # Identity groups per image in two queries total, not one per image.
            # Only ids are needed, so the encrypted embedding is not loaded.
            Prefetch(
                "identity_faces",
                queryset=IdentityFace.objects.only("id", "image").prefetch_related(
                    Prefetch(
                        "memberships",
                        queryset=IdentityMembership.objects.filter(
                            state__in=ACTIVE_IDENTITY_STATES,
                        ).select_related("identity_group"),
                        to_attr="active_memberships",
                    )
                ),
                to_attr="prefetched_identity_faces",
            ),
        )
        .annotate(latest_enhancement_status=Subquery(latest.values("status")[:1]))
    )


@extend_schema_view(get=extend_schema(parameters=[BATCH_FILTER]))
class MyImageListView(generics.ListAPIView):
    serializer_class = MyImageSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = MyImagesPagination

    valid_statuses = {
        "uploaded", "preprocessing", "ready", "pending", "queued",
        "processing", "completed", "partial", "failed", "cancelled", "rejected",
        "accepted", "needs_review",
        "quality_processing", "quality_failed",
    }
    ordering_fields = {
        "newest": "-created_at", "oldest": "created_at", "name": "original_name",
        "-name": "-original_name", "size": "file_size", "-size": "-file_size",
    }

    def get_queryset(self):
        queryset = my_images_queryset(self.request.user)

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(Q(original_name__icontains=search) | Q(identity_faces__memberships__identity_group__display_name__icontains=search)).distinct()

        identity_group = self.request.query_params.get("identity_group")
        if identity_group:
            if not identity_group.isdigit():
                raise ValidationError({"identity_group": "Identity group must be a positive integer."})
            queryset = queryset.filter(identity_faces__memberships__identity_group_id=int(identity_group), identity_faces__memberships__identity_group__owner=self.request.user, identity_faces__memberships__state__in=["auto_matched", "new_group", "confirmed"]).distinct()

        batch = self.request.query_params.get("batch")
        if batch:
            if not batch.isdigit():
                raise ValidationError({"batch": "Batch must be a positive integer."})
            queryset = queryset.filter(batch_id=int(batch))

        upload_type = self.request.query_params.get("upload_type")
        if upload_type:
            if upload_type not in {"single", "multiple", "zip"}:
                raise ValidationError({"upload_type": "Use single, multiple, or zip."})
            queryset = queryset.filter(batch__upload_type=upload_type)

        selected_status = self.request.query_params.get("status", "").strip().lower()
        if selected_status and selected_status != "all":
            if selected_status not in self.valid_statuses:
                raise ValidationError({"status": "Unsupported image status filter."})
            if selected_status == "rejected":
                queryset = queryset.filter(
                    Q(status="rejected") | Q(quality_decision__decision="rejected")
                )
            elif selected_status in {"accepted", "needs_review"}:
                queryset = queryset.filter(quality_decision__decision=selected_status)
            elif selected_status == "quality_processing":
                queryset = queryset.filter(quality_decision__status__in=["pending", "queued", "processing"])
            elif selected_status == "quality_failed":
                queryset = queryset.filter(quality_decision__status="failed")
            elif selected_status == "uploaded":
                queryset = queryset.filter(status="uploaded", preprocessing_result__isnull=True)
            elif selected_status == "preprocessing":
                queryset = queryset.filter(preprocessing_result__status__in=["pending", "processing"])
            elif selected_status == "ready":
                queryset = queryset.filter(
                    preprocessing_result__status="completed",
                    latest_enhancement_status__isnull=True,
                )
            elif selected_status == "failed":
                queryset = queryset.filter(
                    Q(preprocessing_result__status="failed")
                    | Q(latest_enhancement_status="failed")
                    | Q(quality_decision__status="failed")
                )
            else:
                queryset = queryset.filter(latest_enhancement_status=selected_status)

        for parameter, lookup in (
            ("min_score", "quality_decision__overall_score__gte"),
            ("max_score", "quality_decision__overall_score__lte"),
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

        ordering = self.request.query_params.get("ordering", "newest")
        if ordering not in self.ordering_fields:
            raise ValidationError({"ordering": "Use newest, oldest, name, -name, size, or -size."})
        return queryset.order_by(self.ordering_fields[ordering], "-pk")


class ImageUploadDetailView(generics.RetrieveDestroyAPIView):
    """One image, in the same shape the gallery lists it in.

    Deliberately the same serializer as MyImageListView: a photo's own page is
    where someone goes to ask "was it accepted, and why not" -- which needs
    ``display_status`` and the quality decision, neither of which the plain
    upload serializer carries.
    """

    serializer_class = MyImageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return my_images_queryset(self.request.user)


class ImageDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded"),
            pk=pk,
        )
        if not image.original_file:
            return Response({"detail": "The stored image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.ORIGINAL_IMAGE,
            resource_id=image.pk,
            batch_id=image.batch_id,
            image_id=image.pk,
            filename=image.original_name,
            file_size=image.file_size,
            content_type=image.mime_type,
        )
        return FileResponse(
            image.original_file.open("rb"),
            as_attachment=True,
            filename=image.original_name,
            content_type=image.mime_type,
        )


class ImagePreprocessView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_preprocess"

    @extend_schema(request=PreprocessImageRequestSerializer, responses={200: ImagePreprocessingResultSerializer, 201: ImagePreprocessingResultSerializer})
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

    @extend_schema(responses={200: ImagePreprocessingResultSerializer})
    def get(self, request, pk):
        result = get_object_or_404(
            ImagePreprocessingResult.objects.filter(owner=request.user).select_related("image"),
            image_id=pk,
        )
        return Response(ImagePreprocessingResultSerializer(result, context={"request": request}).data)


class ImagePreprocessingDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        result = get_object_or_404(
            ImagePreprocessingResult.objects.filter(owner=request.user, status="completed"),
            image_id=pk,
        )
        if not result.processed_file:
            return Response({"detail": "The processed image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        content_type = "image/png" if result.output_format == "png" else "image/jpeg"
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.STANDARDIZED_IMAGE,
            resource_id=result.pk,
            batch_id=result.image.batch_id,
            image_id=result.image_id,
            filename=result.output_name,
            file_size=result.processed_file_size,
            content_type=content_type,
        )
        return FileResponse(
            result.processed_file.open("rb"),
            as_attachment=True,
            filename=result.output_name,
            content_type=content_type,
        )

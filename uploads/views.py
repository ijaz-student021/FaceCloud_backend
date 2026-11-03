from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, permissions, serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

from enhancement.models import ImageEnhancementResult
from identities.models import IdentityFace, IdentityMembership

from .deletion import delete_images
from quality_control.effective import effective_q

from .display_status import with_status_relations
from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from .progress import batch_progress_payload
from .serializers import (
    BatchProgressSerializer,
    ImagePreprocessingResultSerializer,
    ImageUploadSerializer,
    MyImageSerializer,
    PreprocessImageRequestSerializer,
    UploadBatchCreatedSerializer,
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


ORGANIZATION_FIELD = {"type": "integer", "description": (
    "Optional. File this upload under one of your organizations (you must be an active member) so its moderators can review it. "
    "Without it the upload stays in your own workspace, as before."
)}


AUTO_ENHANCE_FIELD = {"type": "boolean", "description": (
    "Optional. true or false: whether the photographs of this upload that pass the checks are enhanced automatically. Absent means the account setting "
    "(`auto_enhance` of `GET /api/notifications/preferences/`, on unless the person turned it off)."
)}


def _upload_organization(request):
    from organizations.services import resolve_upload_organization

    return resolve_upload_organization(request.user, request.data.get("organization_id"))


def _upload_auto_enhance(request):
    """Whether this upload is enhanced automatically: its own ``auto_enhance`` field, else the account setting (``enhancement/auto.py``)."""
    from enhancement.auto import parse_auto_enhance, resolve_auto_enhance

    try:
        requested = parse_auto_enhance(request.data.get("auto_enhance"))
    except ValueError as error:
        raise ValidationError({"auto_enhance": str(error)}) from error
    return resolve_auto_enhance(request.user, requested)


def _images_with_status():
    return Prefetch("images", queryset=with_status_relations(ImageUpload.objects.select_related("preprocessing_result")))


def _batch_response(request, batch):
    """The upload response: the batch with each photo's status, loaded in a fixed number of queries."""
    batch = UploadBatch.objects.prefetch_related(_images_with_status()).get(pk=batch.pk)
    return Response(UploadBatchCreatedSerializer(batch, context={"request": request}).data, status=status.HTTP_201_CREATED)


class SingleImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "file": {"type": "string", "format": "binary"}, "organization_id": ORGANIZATION_FIELD, "auto_enhance": AUTO_ENHANCE_FIELD,
        }}},
        responses={201: UploadBatchCreatedSerializer},
    )
    def post(self, request):
        files = _all_request_files(request)
        if len(files) != 1:
            raise ValidationError({"file": "Select exactly one image for single-image upload."})
        batch = create_image_batch(
            request.user, "single", files, organization=_upload_organization(request), auto_enhance=_upload_auto_enhance(request),
        )
        return _batch_response(request, batch)


class MultipleImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "string", "format": "binary"}}, "organization_id": ORGANIZATION_FIELD,
            "auto_enhance": AUTO_ENHANCE_FIELD,
        }}},
        responses={201: UploadBatchCreatedSerializer},
    )
    def post(self, request):
        files = request.FILES.getlist("files")
        if len(files) < 2:
            raise ValidationError({"files": "Select at least two images for multiple-image upload."})
        if len(files) != len(_all_request_files(request)):
            raise ValidationError({"files": "Submit all images using the repeated 'files' field."})
        batch = create_image_batch(
            request.user, "multiple", files, organization=_upload_organization(request), auto_enhance=_upload_auto_enhance(request),
        )
        batch = enqueue_batch_preprocessing(batch)
        return _batch_response(request, batch)


class ZipImageUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "zip_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "file": {"type": "string", "format": "binary"}, "organization_id": ORGANIZATION_FIELD, "auto_enhance": AUTO_ENHANCE_FIELD,
        }}},
        responses={201: UploadBatchCreatedSerializer},
    )
    def post(self, request):
        files = _all_request_files(request)
        if len(files) != 1:
            raise ValidationError({"file": "Select exactly one ZIP archive."})
        uploaded_zip = files[0]
        batch = create_zip_batch(
            request.user, uploaded_zip, organization=_upload_organization(request), auto_enhance=_upload_auto_enhance(request),
        )
        batch = enqueue_batch_preprocessing(batch)
        return _batch_response(request, batch)


class UploadBatchListView(generics.ListAPIView):
    serializer_class = UploadBatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UploadBatch.objects.filter(owner=self.request.user).prefetch_related(_images_with_status())


class UploadBatchDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = UploadBatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UploadBatch.objects.filter(owner=self.request.user).prefetch_related(_images_with_status())

    def perform_destroy(self, instance):
        delete_batch(instance)


class UploadBatchProgressView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: BatchProgressSerializer})
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


BULK_DELETE_LIMIT = 200


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
            "batch__organization", "preprocessing_result", "face_validation",
            "quality_decision", "duplicate_analysis",
            "identity_analysis", "place",
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

        taken_from = self.request.query_params.get("taken_from", "").strip()
        taken_to = self.request.query_params.get("taken_to", "").strip()
        if taken_from or taken_to:
            from django.db.models.functions import Coalesce

            from memories.services import parse_date

            queryset = queryset.annotate(captured=Coalesce("taken_at", "created_at"))
            for name, value, lookup in (("taken_from", taken_from, "captured__date__gte"), ("taken_to", taken_to, "captured__date__lte")):
                if value:
                    parsed = parse_date(value)
                    if parsed is None:
                        raise ValidationError({name: "Use a date like 2026-10-22."})
                    queryset = queryset.filter(**{lookup: parsed})

        album = self.request.query_params.get("album")
        if album:
            if not album.isdigit():
                raise ValidationError({"album": "Album must be a positive integer."})
            from albums.models import AlbumImage

            queryset = queryset.filter(Exists(AlbumImage.objects.filter(album_id=int(album), album__owner=self.request.user, image_id=OuterRef("pk"))))

        # ?person=<folder id>, repeated for photographs that contain ALL of those people (a photograph with both Ayesha and Bilal).
        for person in self.request.query_params.getlist("person"):
            if not person.isdigit():
                raise ValidationError({"person": "Person must be a positive integer."})
            from identities.models import IdentityMembership

            queryset = queryset.filter(Exists(IdentityMembership.objects.filter(
                identity_face__image_id=OuterRef("pk"), identity_group_id=int(person), identity_group__owner=self.request.user,
                state__in=["auto_matched", "new_group", "confirmed"],
            )))

        place = self.request.query_params.get("place")
        if place:
            if not place.isdigit():
                raise ValidationError({"place": "Place must be a positive integer."})
            queryset = queryset.filter(place_id=int(place))
        country = self.request.query_params.get("country", "").strip()
        if country:
            if len(country) != 2 or not country.isalpha():
                raise ValidationError({"country": "Use a two-letter country code."})
            queryset = queryset.filter(place__country_code=country.upper())

        favorite = self.request.query_params.get("favorite", "").strip().lower()
        if favorite:
            if favorite not in {"true", "false", "1", "0"}:
                raise ValidationError({"favorite": "Use true or false."})
            queryset = queryset.filter(is_favorite=favorite in {"true", "1"})

        selected_status = self.request.query_params.get("status", "").strip().lower()
        if selected_status and selected_status != "all":
            if selected_status not in self.valid_statuses:
                raise ValidationError({"status": "Unsupported image status filter."})
            if selected_status == "rejected":
                queryset = queryset.filter(
                    Q(status="rejected") | Q(quality_decision__decision="rejected")
                )
            elif selected_status in {"accepted", "needs_review"}:
                # The effective verdict: "needs_review" only where a moderator can decide, and a personal photograph that was doubted counts as accepted.
                queryset = queryset.filter(effective_q(selected_status))
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

    def perform_destroy(self, instance):
        delete_images([instance])


class BulkDeleteRequestSerializer(serializers.Serializer):
    image_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=BULK_DELETE_LIMIT, allow_empty=False,
        help_text=f"Up to {BULK_DELETE_LIMIT} photo ids.",
    )


class BulkDeleteResultSerializer(serializers.Serializer):
    deleted_ids = serializers.ListField(child=serializers.IntegerField())
    not_found_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Ids that are not yours or do not exist: nothing was done for them.")
    folders_updated = serializers.IntegerField(help_text="Person folders whose count and matching were recomputed.")
    folders_deleted = serializers.IntegerField(help_text="Person folders that were left with no photograph and were deleted (the photographs of other folders are untouched).")
    folders_renumbered = serializers.IntegerField(help_text="Folders whose automatic name (Person 004) was renumbered (Person 002) to close the gap the deleted folders left; their ids do not change.")
    packages_expired = serializers.IntegerField(help_text="Ready ZIPs of the affected uploads that were expired because they held a copy.")
    exports_expired = serializers.IntegerField(help_text="Ready person-folder exports that were expired for the same reason.")


class ImageBulkDeleteView(APIView):
    """Delete several of your photographs at once; the effects are those of deleting each (see ``uploads/deletion.py``)."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_delete"

    @extend_schema(request=BulkDeleteRequestSerializer, responses={200: BulkDeleteResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = BulkDeleteRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        wanted = list(dict.fromkeys(payload.validated_data["image_ids"]))
        images = list(ImageUpload.all_objects.filter(owner=request.user, pk__in=wanted))   # permanent: reaches the trash too
        found = {image.pk for image in images}
        summary = delete_images(images)
        return Response({
            "deleted_ids": [pk for pk in wanted if pk in found],
            "not_found_ids": [pk for pk in wanted if pk not in found],
            **{key: value for key, value in summary.items() if key != "deleted"},
        })


class FavoriteResultSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    is_favorite = serializers.BooleanField()
    favorited_at = serializers.DateTimeField(allow_null=True)


class ImageFavoriteView(APIView):
    """Mark one of your photographs as a favourite (``PUT``) or take the mark off (``DELETE``). Both are idempotent."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_favorite"

    def _set(self, request, pk, favorite):
        photo = get_object_or_404(ImageUpload.objects.filter(owner=request.user, status="uploaded"), pk=pk)
        if photo.is_favorite != favorite:
            ImageUpload.objects.filter(pk=photo.pk).update(is_favorite=favorite, favorited_at=timezone.now() if favorite else None)
        photo.refresh_from_db(fields=["is_favorite", "favorited_at"])
        return Response({"id": photo.pk, "is_favorite": photo.is_favorite, "favorited_at": photo.favorited_at})

    @extend_schema(request=None, responses={200: FavoriteResultSerializer, 404: OpenApiTypes.OBJECT})
    def put(self, request, pk):
        return self._set(request, pk, True)

    @extend_schema(request=None, responses={200: FavoriteResultSerializer, 404: OpenApiTypes.OBJECT})
    def delete(self, request, pk):
        return self._set(request, pk, False)


class BulkFavoriteRequestSerializer(serializers.Serializer):
    image_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=BULK_DELETE_LIMIT, allow_empty=False,
        help_text=f"Up to {BULK_DELETE_LIMIT} photo ids.",
    )
    favorite = serializers.BooleanField(help_text="true to mark them as favourites, false to take the mark off.")


class BulkFavoriteResultSerializer(serializers.Serializer):
    favorite = serializers.BooleanField()
    updated_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Photos that now have the requested state (including ones that already had it).")
    not_found_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Ids that are not yours, do not exist or are not stored photographs; nothing was done for them.")


class ImageBulkFavoriteView(APIView):
    """Mark or unmark several of your photographs at once."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_favorite"

    @extend_schema(request=BulkFavoriteRequestSerializer, responses={200: BulkFavoriteResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = BulkFavoriteRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        wanted = list(dict.fromkeys(payload.validated_data["image_ids"]))
        favorite = payload.validated_data["favorite"]
        found = set(ImageUpload.objects.filter(owner=request.user, status="uploaded", pk__in=wanted).values_list("pk", flat=True))
        ImageUpload.objects.filter(pk__in=found).exclude(is_favorite=favorite).update(is_favorite=favorite, favorited_at=timezone.now() if favorite else None)
        return Response({"favorite": favorite, "updated_ids": [pk for pk in wanted if pk in found], "not_found_ids": [pk for pk in wanted if pk not in found]})


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
            ImagePreprocessingResult.objects.filter(owner=request.user, image__trashed_at__isnull=True).select_related("image"),
            image_id=pk,
        )
        return Response(ImagePreprocessingResultSerializer(result, context={"request": request}).data)


class ImagePreprocessingDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        result = get_object_or_404(
            ImagePreprocessingResult.objects.filter(owner=request.user, status="completed", image__trashed_at__isnull=True),
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


class UploadLimitsSerializer(serializers.Serializer):
    accepted_extensions = serializers.ListField(child=serializers.CharField())
    accepted_mime_types = serializers.ListField(child=serializers.CharField())
    max_image_bytes = serializers.IntegerField()
    max_image_pixels = serializers.IntegerField()
    max_files_per_batch = serializers.IntegerField(help_text="Multiple upload and ZIP entries alike.")
    max_zip_bytes = serializers.IntegerField()
    max_zip_uncompressed_bytes = serializers.IntegerField()
    max_zip_compression_ratio = serializers.IntegerField()
    enhancement_max_input_pixels = serializers.IntegerField(
        help_text="Larger originals are enhanced at a reduced size; the original is kept unchanged.",
    )
    rate_limits = serializers.DictField(child=serializers.CharField(), help_text="Per user, e.g. '100/hour'.")


class UploadLimitsView(APIView):
    """The limits upload validation enforces, for clients to state and pre-check.

    Read from the same settings and format table the validators use. A client
    pre-check is a convenience: the server validates every upload regardless.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: UploadLimitsSerializer})
    def get(self, request):
        from .validators import ALLOWED_EXTENSIONS, FORMAT_DETAILS

        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        return Response(UploadLimitsSerializer({
            "accepted_extensions": sorted(ALLOWED_EXTENSIONS),
            "accepted_mime_types": sorted({mime for _extension, mime in FORMAT_DETAILS.values()}),
            "max_image_bytes": settings.MAX_IMAGE_UPLOAD_SIZE,
            "max_image_pixels": settings.MAX_IMAGE_PIXELS,
            "max_files_per_batch": settings.MAX_UPLOAD_BATCH_FILES,
            "max_zip_bytes": settings.MAX_ZIP_UPLOAD_SIZE,
            "max_zip_uncompressed_bytes": settings.MAX_ZIP_UNCOMPRESSED_SIZE,
            "max_zip_compression_ratio": settings.MAX_ZIP_COMPRESSION_RATIO,
            "enhancement_max_input_pixels": settings.ENHANCEMENT_MAX_INPUT_PIXELS,
            "rate_limits": {scope: rates[scope] for scope in ("image_upload", "zip_upload") if scope in rates},
        }).data)

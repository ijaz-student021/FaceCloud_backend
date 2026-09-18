import uuid

from django.db import IntegrityError, transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload, UploadBatch

from .models import (
    EnhancementBatchJob,
    EnhancementStatus,
    ImageEditVersion,
    ImageEnhancementResult,
)
from .serializers import (
    EnhancementBatchJobSerializer,
    EnhancementRequestSerializer,
    GalleryEditRequestSerializer,
    ImageEditVersionSerializer,
    ImageEnhancementComparisonSerializer,
    ImageEnhancementResultSerializer,
)
from .services import create_enhancement_result
from .tasks import (
    process_enhancement_batch,
    process_enhancement_image,
    process_gallery_edit,
)


class ImageEnhanceView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=EnhancementRequestSerializer,
        responses={202: ImageEnhancementResultSerializer},
    )
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded").select_related("preprocessing_result"),
            pk=image_id,
        )
        payload = EnhancementRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            result = create_enhancement_result(image, payload.validated_data["level"])
        except IntegrityError as error:
            raise ValidationError("This image already has an active enhancement operation.") from error
        task_id = uuid.uuid4().hex
        result.status = EnhancementStatus.QUEUED
        result.task_id = task_id
        result.save(update_fields=["status", "task_id", "updated_at"])
        try:
            process_enhancement_image.apply_async(args=[result.pk], task_id=task_id)
        except Exception:
            result.status = EnhancementStatus.FAILED
            result.failure_reason = "The background enhancement queue is unavailable."
            result.finished_at = timezone.now()
            result.save(
                update_fields=[
                    "status", "failure_reason", "finished_at", "updated_at",
                ]
            )
        result.refresh_from_db()
        return Response(
            ImageEnhancementResultSerializer(result, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED,
        )


class BatchEnhanceView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=EnhancementRequestSerializer,
        responses={201: EnhancementBatchJobSerializer},
    )
    def post(self, request, batch_id):
        batch = get_object_or_404(UploadBatch.objects.filter(owner=request.user), pk=batch_id)
        payload = EnhancementRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        images = list(
            batch.images.filter(status="uploaded", preprocessing_result__status="completed")
            .select_related("preprocessing_result", "owner")
        )
        if not images:
            raise ValidationError("This batch has no preprocessed images ready for enhancement.")
        job = EnhancementBatchJob.objects.create(
            owner=request.user,
            source_batch=batch,
            requested_level=payload.validated_data["level"],
            status=EnhancementStatus.PENDING,
            total_images=len(images),
        )
        try:
            for image in images:
                create_enhancement_result(image, job.requested_level, batch_job=job)
        except Exception:
            job.delete()
            raise ValidationError("One or more images already have an active enhancement operation.")
        task_id = uuid.uuid4().hex
        job.status = EnhancementStatus.QUEUED
        job.task_id = task_id
        job.save(update_fields=["status", "task_id", "updated_at"])
        try:
            process_enhancement_batch.apply_async(args=[job.pk], task_id=task_id)
        except Exception:
            job.status = EnhancementStatus.FAILED
            job.failure_reason = "The background enhancement queue is unavailable."
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
        job.refresh_from_db()
        return Response(
            EnhancementBatchJobSerializer(job, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class EnhancementResultListView(generics.ListAPIView):
    serializer_class = ImageEnhancementResultSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ImageEnhancementResult.objects.filter(owner=self.request.user).select_related(
            "image", "batch_job"
        ).prefetch_related("edit_versions")


class EnhancementResultDetailView(generics.RetrieveAPIView):
    serializer_class = ImageEnhancementResultSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ImageEnhancementResult.objects.filter(
            owner=self.request.user
        ).prefetch_related("edit_versions")


class EnhancementJobListView(generics.ListAPIView):
    serializer_class = EnhancementBatchJobSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return EnhancementBatchJob.objects.filter(owner=self.request.user).prefetch_related("results")


class EnhancementJobDetailView(generics.RetrieveAPIView):
    serializer_class = EnhancementBatchJobSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return EnhancementBatchJob.objects.filter(owner=self.request.user).prefetch_related("results")


class EnhancementDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        result = get_object_or_404(
            ImageEnhancementResult.objects.filter(owner=request.user, status=EnhancementStatus.COMPLETED),
            pk=pk,
        )
        if not result.enhanced_file:
            return Response({"detail": "The enhanced image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        content_type = "image/png" if result.output_format == "png" else "image/jpeg"
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.ENHANCED_IMAGE,
            resource_id=result.pk,
            batch_id=result.image.batch_id,
            image_id=result.image_id,
            filename=result.output_name,
            file_size=result.output_file_size,
            content_type=content_type,
        )
        return FileResponse(
            result.enhanced_file.open("rb"), as_attachment=True,
            filename=result.output_name, content_type=content_type,
        )


class EnhancementComparisonView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _stored_file_exists(file_field):
        if not file_field or not file_field.name:
            return False
        try:
            return file_field.storage.exists(file_field.name)
        except (OSError, ValueError):
            return False

    @extend_schema(
        responses={
            200: ImageEnhancementComparisonSerializer,
            409: OpenApiTypes.OBJECT,
            410: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request, pk):
        result = get_object_or_404(
            ImageEnhancementResult.objects.filter(owner=request.user).select_related(
                "image", "preprocessing_result"
            ),
            pk=pk,
        )
        if result.status != EnhancementStatus.COMPLETED:
            payload = {
                "detail": "Comparison is available only after enhancement completes.",
                "status": result.status,
            }
            if result.status == EnhancementStatus.FAILED and result.failure_reason:
                payload["failure_reason"] = result.failure_reason
            return Response(payload, status=status.HTTP_409_CONFLICT)

        required_files = (
            result.image.original_file,
            result.preprocessing_result.processed_file,
            result.enhanced_file,
        )
        if not all(self._stored_file_exists(file_field) for file_field in required_files):
            return Response(
                {"detail": "One or more comparison images are no longer available."},
                status=status.HTTP_410_GONE,
            )

        comparison = result.applied_parameters.get("quality_comparison")
        if not isinstance(comparison, dict) or not comparison.get("method"):
            return Response(
                {"detail": "Comparison measurements are unavailable for this result."},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = ImageEnhancementComparisonSerializer(
            result,
            context={"request": request},
        )
        return Response(serializer.data)


class GalleryEditorConfigurationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(
            {
                "modes": ["auto", "manual"],
                "source": "completed_enhanced_image",
                "non_destructive": True,
                "preview_strategy": "client_side_preview_then_server_render",
                "controls": {
                    name: {
                        "minimum": -100,
                        "maximum": 100,
                        "step": 1,
                        "default": 0,
                        "recommended_minimum": -50,
                        "recommended_maximum": 50,
                    }
                    for name in (
                        "brightness", "contrast", "shadows", "saturation",
                        "highlights",
                    )
                },
            }
        )


class GalleryEditListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _result(self, request, result_id):
        return get_object_or_404(
            ImageEnhancementResult.objects.filter(
                owner=request.user,
                status=EnhancementStatus.COMPLETED,
            ).select_related("preprocessing_result"),
            pk=result_id,
        )

    @extend_schema(responses={200: ImageEditVersionSerializer(many=True)})
    def get(self, request, result_id):
        result = self._result(request, result_id)
        edits = result.edit_versions.filter(owner=request.user)
        return Response(
            ImageEditVersionSerializer(
                edits, many=True, context={"request": request}
            ).data
        )

    @extend_schema(
        request=GalleryEditRequestSerializer,
        responses={202: ImageEditVersionSerializer, 410: OpenApiTypes.OBJECT},
    )
    def post(self, request, result_id):
        result = self._result(request, result_id)
        if not result.enhanced_file:
            return Response(
                {"detail": "The enhanced image is unavailable."},
                status=status.HTTP_410_GONE,
            )
        payload = GalleryEditRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        task_id = uuid.uuid4().hex
        try:
            edit = ImageEditVersion.objects.create(
                owner=request.user,
                enhancement_result=result,
                mode=payload.validated_data["mode"],
                requested_adjustments=payload.validated_data["adjustments"],
                status=EnhancementStatus.QUEUED,
                task_id=task_id,
            )
        except IntegrityError as error:
            raise ValidationError(
                "This image already has an active gallery-edit operation."
            ) from error
        try:
            process_gallery_edit.apply_async(args=[edit.pk], task_id=task_id)
        except Exception:
            edit.status = EnhancementStatus.FAILED
            edit.failure_reason = "The background gallery-edit queue is unavailable."
            edit.finished_at = timezone.now()
            edit.save(
                update_fields=[
                    "status", "failure_reason", "finished_at", "updated_at",
                ]
            )
        edit.refresh_from_db()
        return Response(
            ImageEditVersionSerializer(
                edit, context={"request": request}
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class GalleryEditDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _edit(self, request, pk):
        return get_object_or_404(
            ImageEditVersion.objects.filter(owner=request.user), pk=pk
        )

    @extend_schema(responses={200: ImageEditVersionSerializer})
    def get(self, request, pk):
        edit = self._edit(request, pk)
        return Response(
            ImageEditVersionSerializer(edit, context={"request": request}).data
        )

    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        edit = self._edit(request, pk)
        result_id = edit.enhancement_result_id
        was_current = edit.is_current
        edit.delete()
        if was_current:
            replacement = ImageEditVersion.objects.filter(
                owner=request.user,
                enhancement_result_id=result_id,
                status=EnhancementStatus.COMPLETED,
            ).first()
            if replacement:
                replacement.is_current = True
                replacement.save(update_fields=["is_current", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class GalleryEditActivateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: ImageEditVersionSerializer})
    def post(self, request, pk):
        edit = get_object_or_404(
            ImageEditVersion.objects.filter(
                owner=request.user,
                status=EnhancementStatus.COMPLETED,
            ),
            pk=pk,
        )
        with transaction.atomic():
            ImageEditVersion.objects.filter(
                enhancement_result_id=edit.enhancement_result_id,
                is_current=True,
            ).exclude(pk=edit.pk).update(is_current=False)
            edit.is_current = True
            edit.save(update_fields=["is_current", "updated_at"])
        return Response(
            ImageEditVersionSerializer(edit, context={"request": request}).data
        )


class GalleryEditDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        edit = get_object_or_404(
            ImageEditVersion.objects.filter(
                owner=request.user,
                status=EnhancementStatus.COMPLETED,
            ).select_related("enhancement_result__image"),
            pk=pk,
        )
        if not edit.edited_file:
            return Response(
                {"detail": "The edited image is unavailable."},
                status=status.HTTP_404_NOT_FOUND,
            )
        content_type = "image/png" if edit.output_format == "png" else "image/jpeg"
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.EDITED_IMAGE,
            resource_id=edit.pk,
            batch_id=edit.enhancement_result.image.batch_id,
            image_id=edit.enhancement_result.image_id,
            filename=edit.output_name,
            file_size=edit.output_file_size,
            content_type=content_type,
        )
        return FileResponse(
            edit.edited_file.open("rb"),
            as_attachment=True,
            filename=edit.output_name,
            content_type=content_type,
        )

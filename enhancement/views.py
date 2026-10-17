import io
import logging
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload, UploadBatch

from .models import (
    EditPreview,
    EnhancementBatchJob,
    EnhancementStatus,
    ImageEditVersion,
    ImageEnhancementResult,
    ModelComparison,
    ModelComparisonOutput,
)
from .comparison import (
    build_report_csv,
    ensure_comparison_capacity,
    validate_profiles,
)
from .serializers import (
    EditPreviewSerializer,
    PreviewListSerializer,
    PreviewRequestSerializer,
    EditorConfigurationSerializer,
    EnhancementBatchJobSerializer,
    EnhancementRequestSerializer,
    GalleryEditRequestSerializer,
    ImageEditVersionSerializer,
    ImageEnhancementComparisonSerializer,
    ImageEnhancementResultSerializer,
    ModelComparisonRequestSerializer,
    ModelComparisonSerializer,
    ModelComparisonSummarySerializer,
    WatermarkOptionsSerializer,
)
from . import policy as rendering_policy
from .controls import INTENTS, RENDERER_VERSION, neutral_settings
from .controls import contract as editor_contract
from .editor_source import source_version, transparency_policy
from .previews import StaleSource, create_preview
from .batch_jobs import start_batch_enhancement
from .services import create_enhancement_result, ensure_enhancement_capacity
from .watermark import WatermarkError, render_watermarked
from .tasks import (
    process_enhancement_image,
    process_gallery_edit,
    run_model_comparison,
)


PREVIEW_MAX_EDGE = 1024
WATERMARK_PARAMETERS = [
    OpenApiParameter("watermark", OpenApiTypes.STR, description="none (default), small, facecloud, username or custom."),
    OpenApiParameter("watermark_position", OpenApiTypes.STR, description="bottom_right (default), bottom_left, top_right, top_left, center or tile."),
    OpenApiParameter("watermark_opacity", OpenApiTypes.FLOAT, description="0.1 to 1.0, default 0.5."),
    OpenApiParameter("watermark_text", OpenApiTypes.STR, description="Text for watermark=custom (40 Latin characters at most)."),
]


def _watermark_spec(request):
    options = WatermarkOptionsSerializer(data=request.query_params)
    options.is_valid(raise_exception=True)
    return options.validated_data["spec"]


def _watermarked_file(file_field, output_format, spec, user, max_edge=None):
    try:
        return render_watermarked(file_field, output_format, spec, user, max_edge=max_edge)
    except WatermarkError as error:
        raise ValidationError({"watermark": [str(error)]}) from error
    except OSError as error:
        raise ValidationError({"watermark": ["The image could not be watermarked."]}) from error


def _watermarked_name(name):
    stem, dot, extension = name.rpartition(".")
    return f"{stem}_watermarked.{extension}" if dot else f"{name}_watermarked"



logger = logging.getLogger(__name__)

class ImageEnhanceView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Enhance one image",
        description=(
            "Queues an enhancement. `style` picks the look (natural, dynamic, ai_max); "
            "`level` the strength. Returns 202 immediately; poll the result for progress."
        ),
        request=EnhancementRequestSerializer,
        responses={
            202: ImageEnhancementResultSerializer,
            400: OpenApiTypes.OBJECT,
            429: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample("Natural, automatic strength", value={"level": "auto", "style": "natural"}, request_only=True),
            OpenApiExample("Full AI enhance", value={"level": "high", "style": "ai_max"}, request_only=True),
            OpenApiExample(
                "Too many enhancements in progress",
                value={"detail": "You already have 10 image enhancements in progress (limit 10). Wait for some to finish, then try again."},
                response_only=True, status_codes=["429"],
            ),
        ],
    )
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded").select_related("preprocessing_result"),
            pk=image_id,
        )
        payload = EnhancementRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ensure_enhancement_capacity(request.user, batch=False)
        try:
            result = create_enhancement_result(
                image, payload.validated_data["level"],
                requested_style=payload.validated_data["style"],
            )
        except IntegrityError as error:
            raise ValidationError("This image already has an active enhancement operation.") from error
        task_id = uuid.uuid4().hex
        result.status = EnhancementStatus.QUEUED
        result.task_id = task_id
        result.save(update_fields=["status", "task_id", "updated_at"])
        try:
            process_enhancement_image.apply_async(args=[result.pk], task_id=task_id)
        except Exception:
            logger.exception("Enhancement result %s could not be queued.", result.pk)
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
        summary="Enhance every ready image in a batch",
        description=(
            "409 lists the images that already have an enhancement queued or running (nothing is "
            "created). 500 carries only an error_reference; the cause is logged on the server."
        ),
        request=EnhancementRequestSerializer,
        responses={
            201: EnhancementBatchJobSerializer,
            400: OpenApiTypes.OBJECT,
            409: OpenApiTypes.OBJECT,
            429: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample("Dynamic look", value={"level": "medium", "style": "dynamic"}, request_only=True),
            OpenApiExample(
                "Images already in progress",
                value={"detail": "Some images in this batch already have an enhancement queued or running.",
                       "image_ids": [41, 42]},
                response_only=True, status_codes=["409"],
            ),
            OpenApiExample(
                "Unexpected failure",
                value={"detail": "The batch enhancement could not be started. Quote the reference if you "
                                 "contact support.", "error_reference": "3f9a1c0b7e2d"},
                response_only=True, status_codes=["500"],
            ),
            OpenApiExample(
                "Too many batch jobs in progress",
                value={"detail": "You already have 2 batch enhancement jobs in progress (limit 2). Wait for some to finish, then try again."},
                response_only=True, status_codes=["429"],
            ),
        ],
    )
    def post(self, request, batch_id):
        batch = get_object_or_404(UploadBatch.objects.filter(owner=request.user), pk=batch_id)
        payload = EnhancementRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ensure_enhancement_capacity(request.user, batch=True)
        job = start_batch_enhancement(
            batch, level=payload.validated_data["level"], style=payload.validated_data["style"],
        )
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

    @extend_schema(
        parameters=WATERMARK_PARAMETERS,
        responses={200: OpenApiTypes.BINARY, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        result = get_object_or_404(
            ImageEnhancementResult.objects.filter(owner=request.user, status=EnhancementStatus.COMPLETED),
            pk=pk,
        )
        if not result.enhanced_file:
            return Response({"detail": "The enhanced image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        spec = _watermark_spec(request)
        content_type = "image/png" if result.output_format == "png" else "image/jpeg"
        filename, file_size, stream = result.output_name, result.output_file_size, None
        if spec.active:
            payload, content_type = _watermarked_file(
                result.enhanced_file, result.output_format, spec, request.user,
            )
            filename, file_size, stream = _watermarked_name(filename), len(payload), io.BytesIO(payload)
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.ENHANCED_IMAGE,
            resource_id=result.pk,
            batch_id=result.image.batch_id,
            image_id=result.image_id,
            filename=filename,
            file_size=file_size,
            content_type=content_type,
        )
        return FileResponse(
            stream or result.enhanced_file.open("rb"), as_attachment=True,
            filename=filename, content_type=content_type,
        )


class EnhancementWatermarkPreviewView(APIView):
    """Downscaled inline preview of a watermark, so it can be checked before export."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        parameters=WATERMARK_PARAMETERS,
        responses={200: OpenApiTypes.BINARY, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        result = get_object_or_404(
            ImageEnhancementResult.objects.filter(owner=request.user, status=EnhancementStatus.COMPLETED),
            pk=pk,
        )
        if not result.enhanced_file:
            return Response({"detail": "The enhanced image is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        spec = _watermark_spec(request)
        if not spec.active:
            raise ValidationError({"watermark": ["Choose a watermark mode to preview."]})
        payload, content_type = _watermarked_file(
            result.enhanced_file, result.output_format, spec, request.user,
            max_edge=PREVIEW_MAX_EDGE,
        )
        response = HttpResponse(payload, content_type=content_type)
        response["Cache-Control"] = "private, no-store"
        return response


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
    """The editing contract: controls, ranges, neutral values, renderer and policy.

    Reset sets ``neutral_settings``; Auto and presets are separate requests.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: EditorConfigurationSerializer})
    def get(self, request):
        return Response(
            {
                "modes": ["auto", "manual"],
                "source": "completed_enhanced_image",
                "non_destructive": True,
                "preview_strategy": "server_preview_revisions_then_full_resolution_save",
                "preview": {
                    "default_edge": settings.ENHANCEMENT_PREVIEW_DEFAULT_EDGE,
                    "max_edge": settings.ENHANCEMENT_PREVIEW_MAX_EDGE,
                    "retention_hours": settings.ENHANCEMENT_PREVIEW_RETENTION_HOURS,
                    "kept_per_image": settings.ENHANCEMENT_PREVIEW_KEEP_PER_RESULT,
                    "source": "a bounded lossless copy of the enhanced image, cached per source_version",
                    "revisions": "chosen by the client; the highest is the latest; older ones never overwrite newer ones",
                },
                "transparency": transparency_policy("png or jpeg, as the result"),
                "renderer_version": RENDERER_VERSION,
                "render_policy": rendering_policy.describe(),
                "intents": list(INTENTS),
                "neutral_settings": neutral_settings(),
                "controls": editor_contract(),
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
        responses={202: ImageEditVersionSerializer, 400: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT,
                   410: OpenApiTypes.OBJECT},
        description=(
            "Renders a full-resolution edit version from the enhanced image, in the background. "
            "409 stale_source when source_version is given and no longer current. Only one edit per "
            "image may be in progress (400)."
        ),
    )
    def post(self, request, result_id):
        result = self._result(request, result_id)
        payload = GalleryEditRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return start_edit(
            request, result, payload.validated_data["mode"], payload.validated_data["adjustments"],
            claimed_version=payload.validated_data.get("source_version", ""),
        )


def start_edit(request, result, mode, adjustments, *, claimed_version="", preview=None, advisor_recommendation=None):
    """Queue a full-resolution edit of ``result``; shared by a direct save and a save from a preview."""
    if not result.enhanced_file:
        return Response({"detail": "The enhanced image is unavailable."}, status=status.HTTP_410_GONE)
    version = source_version(result)
    if claimed_version and claimed_version != version:
        raise StaleSource(version)
    task_id = uuid.uuid4().hex
    try:
        edit = ImageEditVersion.objects.create(
            owner=request.user, enhancement_result=result, mode=mode, requested_adjustments=adjustments,
            status=EnhancementStatus.QUEUED, task_id=task_id, source_version=version, from_preview=preview,
            advisor_recommendation=advisor_recommendation,
        )
    except IntegrityError as error:
        raise ValidationError("This image already has an active gallery-edit operation.") from error
    try:
        process_gallery_edit.apply_async(args=[edit.pk], task_id=task_id)
    except Exception:
        logger.exception("Gallery edit %s could not be queued.", edit.pk)
        edit.status = EnhancementStatus.FAILED
        edit.failure_reason = "The background gallery-edit queue is unavailable."
        edit.finished_at = timezone.now()
        edit.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
    edit.refresh_from_db()
    return Response(
        ImageEditVersionSerializer(edit, context={"request": request}).data, status=status.HTTP_202_ACCEPTED,
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

    @extend_schema(
        parameters=WATERMARK_PARAMETERS,
        responses={200: OpenApiTypes.BINARY, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
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
        spec = _watermark_spec(request)
        content_type = "image/png" if edit.output_format == "png" else "image/jpeg"
        filename, file_size, stream = edit.output_name, edit.output_file_size, None
        if spec.active:
            payload, content_type = _watermarked_file(
                edit.edited_file, edit.output_format, spec, request.user,
            )
            filename, file_size, stream = _watermarked_name(filename), len(payload), io.BytesIO(payload)
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.EDITED_IMAGE,
            resource_id=edit.pk,
            batch_id=edit.enhancement_result.image.batch_id,
            image_id=edit.enhancement_result.image_id,
            filename=filename,
            file_size=file_size,
            content_type=content_type,
        )
        return FileResponse(
            stream or edit.edited_file.open("rb"),
            as_attachment=True,
            filename=filename,
            content_type=content_type,
        )


class ImageCompareView(APIView):
    """Start a side-by-side comparison of enhancement profiles on one image."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "model_comparison"

    @extend_schema(
        summary="Compare enhancement profiles on one image",
        description=(
            "Runs the chosen profiles on a downscaled copy and returns 202 immediately. "
            "Poll the comparison for metrics, previews and a recommendation."
        ),
        request=ModelComparisonRequestSerializer,
        responses={
            202: ModelComparisonSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample("All profiles", value={"level": "auto"}, request_only=True),
            OpenApiExample(
                "Two profiles", value={"level": "auto", "profiles": ["opencv_baseline", "natural"]},
                request_only=True,
            ),
        ],
    )
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user, status="uploaded")
            .select_related("preprocessing_result"),
            pk=image_id,
        )
        payload = ModelComparisonRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        preprocessing = getattr(image, "preprocessing_result", None)
        if preprocessing is None or preprocessing.status != "completed":
            raise ValidationError("The image must finish preprocessing before it can be compared.")
        profiles = validate_profiles(payload.validated_data.get("profiles"))
        ensure_comparison_capacity(request.user)
        task_id = uuid.uuid4().hex
        comparison = ModelComparison.objects.create(
            owner=request.user, image=image, profiles=profiles,
            requested_level=payload.validated_data["level"],
            status=EnhancementStatus.QUEUED, task_id=task_id,
        )
        try:
            run_model_comparison.apply_async(args=[comparison.pk], task_id=task_id)
        except Exception:
            comparison.status = EnhancementStatus.FAILED
            comparison.failure_reason = "The background queue is unavailable."
            comparison.finished_at = timezone.now()
            comparison.save(update_fields=["status", "failure_reason", "finished_at", "updated_at"])
        return Response(
            ModelComparisonSerializer(comparison, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ModelComparisonListView(generics.ListAPIView):
    serializer_class = ModelComparisonSummarySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ModelComparison.objects.filter(owner=self.request.user)


class ModelComparisonDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = ModelComparisonSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ModelComparison.objects.filter(owner=self.request.user).prefetch_related("outputs")


class ModelComparisonPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk, profile):
        output = get_object_or_404(
            ModelComparisonOutput.objects.filter(
                comparison__owner=request.user, comparison_id=pk,
                outcome=ModelComparisonOutput.Outcome.COMPLETED,
            ),
            profile=profile,
        )
        if not output.preview_file:
            return Response({"detail": "The preview is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(output.preview_file.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "private, no-store"
        return response


class ModelComparisonReportView(APIView):
    """CSV evaluation report: one row per profile plus the untouched original."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.STR, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        comparison = get_object_or_404(
            ModelComparison.objects.filter(
                owner=request.user, status=EnhancementStatus.COMPLETED,
            ).prefetch_related("outputs"),
            pk=pk,
        )
        response = HttpResponse(build_report_csv(comparison), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="model-comparison-{comparison.pk}.csv"'
        return response


class EditPreviewListCreateView(APIView):
    """Bounded previews of an enhanced image, one immutable image per revision."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "edit_preview"

    def _result(self, request, result_id):
        return get_object_or_404(
            ImageEnhancementResult.objects.filter(owner=request.user, status=EnhancementStatus.COMPLETED),
            pk=result_id,
        )

    @extend_schema(responses={200: PreviewListSerializer})
    def get(self, request, result_id):
        result = self._result(request, result_id)
        previews = list(EditPreview.objects.filter(owner=request.user, enhancement_result=result)
                        .select_related("source", "owner", "enhancement_result")[:20])
        return Response(PreviewListSerializer({
            "source_version": source_version(result),
            "latest_revision": previews[0].revision if previews else None,
            "previews": previews,
        }, context={"request": request}).data)

    @extend_schema(
        request=PreviewRequestSerializer,
        responses={201: EditPreviewSerializer, 200: EditPreviewSerializer, 400: OpenApiTypes.OBJECT,
                   409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
        description=(
            "201 when rendered, 200 when the same revision with the same settings already exists. "
            "409 stale_source (source_version is no longer current) or revision_conflict (the revision was "
            "used with different settings). Closing the connection does not cancel the render."
        ),
    )
    def post(self, request, result_id):
        result = self._result(request, result_id)
        if not result.enhanced_file:
            return Response({"detail": "The enhanced image is unavailable."}, status=status.HTTP_410_GONE)
        payload = PreviewRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        preview, created = create_preview(
            result, request.user, revision=data["revision"], mode=data["mode"], adjustments=data["adjustments"],
            size=data.get("size"), claimed_version=data.get("source_version", ""),
        )
        return Response(
            EditPreviewSerializer(preview, context={"request": request}).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class EditPreviewDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _preview(self, request, pk):
        return get_object_or_404(
            EditPreview.objects.filter(owner=request.user).select_related("source", "enhancement_result", "owner"),
            pk=pk,
        )

    @extend_schema(responses={200: EditPreviewSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        return Response(EditPreviewSerializer(self._preview(request, pk), context={"request": request}).data)

    @extend_schema(responses={204: None, 404: OpenApiTypes.OBJECT})
    def delete(self, request, pk):
        self._preview(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class EditPreviewImageView(APIView):
    """The preview's own image. Authenticated, owner only; each id is immutable."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={(200, "image/jpeg"): OpenApiTypes.BINARY, (200, "image/png"): OpenApiTypes.BINARY,
                              404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        preview = get_object_or_404(EditPreview.objects.filter(owner=request.user), pk=pk)
        content_type = "image/png" if preview.output_format == "png" else "image/jpeg"
        response = FileResponse(preview.file.open("rb"), content_type=content_type)
        response["Cache-Control"] = "private, max-age=3600"
        response["Content-Disposition"] = f'inline; filename="preview-{preview.revision}.{ "png" if preview.output_format == "png" else "jpg"}"'
        return response


class EditPreviewSaveView(APIView):
    """Save a preview at full resolution: a new edit version from the same settings, in the background."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=None,
        responses={202: ImageEditVersionSerializer, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT,
                   409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
        description="409 stale_source when the image changed since the preview was made.",
    )
    def post(self, request, pk):
        preview = get_object_or_404(
            EditPreview.objects.filter(owner=request.user).select_related("source", "enhancement_result"), pk=pk,
        )
        result = preview.enhancement_result
        if result.status != EnhancementStatus.COMPLETED:
            raise Http404("The enhanced image is not available.")
        return start_edit(
            request, result, preview.mode, preview.requested_adjustments,
            claimed_version=preview.source.source_version, preview=preview,
        )

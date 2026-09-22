import re
from pathlib import Path

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload, UploadBatch

from .models import ProcessingReport
from .serializers import ProcessingReportRequestSerializer, ProcessingReportSerializer
from .services import ReportNotReadyError, enqueue_processing_report


class ReportNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Processing must finish before a report can be generated."
    default_code = "report_not_ready"


class ReportPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _enqueue_response(request, *, image=None, batch=None, report_format):
    try:
        report, created = enqueue_processing_report(
            owner=request.user,
            report_format=report_format,
            image=image,
            batch=batch,
        )
    except ReportNotReadyError as error:
        raise ReportNotReady(str(error)) from error
    serializer = ProcessingReportSerializer(report, context={"request": request})
    response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return Response(
        {"created": created, "report": serializer.data},
        status=response_status,
    )


class ImageReportCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "report_generation"

    @extend_schema(
        request=ProcessingReportRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, image_id):
        image = get_object_or_404(
            ImageUpload.objects.filter(owner=request.user).select_related(
                "batch", "preprocessing_result", "quality_decision",
                "processing_progress",
            ),
            pk=image_id,
        )
        payload = ProcessingReportRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return _enqueue_response(
            request,
            image=image,
            report_format=payload.validated_data["format"],
        )


class BatchReportCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "report_generation"

    @extend_schema(
        request=ProcessingReportRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, batch_id):
        batch = get_object_or_404(
            UploadBatch.objects.filter(owner=request.user),
            pk=batch_id,
        )
        payload = ProcessingReportRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return _enqueue_response(
            request,
            batch=batch,
            report_format=payload.validated_data["format"],
        )


class ProcessingReportListView(generics.ListAPIView):
    serializer_class = ProcessingReportSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = ReportPagination

    def get_queryset(self):
        queryset = ProcessingReport.objects.filter(owner=self.request.user).select_related(
            "image", "batch",
        )
        filters = (
            ("status", ProcessingReport.Status.values),
            ("format", ProcessingReport.Format.values),
            ("report_type", ProcessingReport.ReportType.values),
        )
        for parameter, allowed_values in filters:
            value = self.request.query_params.get(parameter, "").strip().lower()
            if not value:
                continue
            if value not in allowed_values:
                raise ValidationError({parameter: f"Unsupported {parameter} filter."})
            queryset = queryset.filter(**{parameter: value})
        return queryset


class ProcessingReportDetailView(generics.RetrieveAPIView):
    serializer_class = ProcessingReportSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ProcessingReport.objects.filter(owner=self.request.user).select_related(
            "image", "batch",
        )


def _download_filename(report):
    source_name = (
        report.image.original_name
        if report.image_id
        else report.batch.display_name
        or report.batch.original_archive_name
        or f"batch-{report.batch_id}"
    )
    stem = Path(source_name).stem[:80]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return f"facecloud-{safe_stem or report.report_type}-report.{report.format}"


class ProcessingReportDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses={200: OpenApiTypes.BINARY, 409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        report = get_object_or_404(
            ProcessingReport.objects.filter(owner=request.user).select_related(
                "image", "batch",
            ),
            pk=pk,
        )
        if report.status != ProcessingReport.Status.READY:
            raise ReportNotReady("The report file is not ready for download.")
        if not report.report_file or not report.report_file.storage.exists(
            report.report_file.name
        ):
            return Response(
                {"detail": "The generated report file is unavailable."},
                status=status.HTTP_410_GONE,
            )
        from downloads.models import DownloadEvent
        from downloads.services import record_download
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.PROCESSING_REPORT,
            resource_id=report.pk,
            batch_id=report.batch_id or report.image.batch_id,
            image_id=report.image_id,
            filename=_download_filename(report),
            file_size=report.file_size,
            content_type=report.content_type,
        )
        return FileResponse(
            report.report_file.open("rb"),
            as_attachment=True,
            filename=_download_filename(report),
            content_type=report.content_type,
        )


class ProcessingReportRegenerateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "report_generation"

    @extend_schema(
        request=ProcessingReportRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        report = get_object_or_404(
            ProcessingReport.objects.filter(owner=request.user).select_related(
                "image__batch", "batch",
            ),
            pk=pk,
        )
        payload = ProcessingReportRequestSerializer(
            data=request.data or {"format": report.format}
        )
        payload.is_valid(raise_exception=True)
        return _enqueue_response(
            request,
            image=report.image if report.image_id else None,
            batch=report.batch if report.batch_id else None,
            report_format=payload.validated_data["format"],
        )

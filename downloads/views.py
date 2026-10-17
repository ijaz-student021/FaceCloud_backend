from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import UploadBatch

from .enrichment import build_extras
from .models import DownloadEvent, DownloadPackage
from .serializers import (
    DownloadEventSerializer,
    DownloadPackageRequestSerializer,
    DownloadPackageSerializer,
)
from .services import (
    PackageNotReadyError,
    enqueue_download_package,
    expire_due_packages,
    expire_package_if_needed,
    record_download,
)


class PackageNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The requested download package is not ready."
    default_code = "package_not_ready"


class PackageNotReadyWithReason(PackageNotReady):
    """409 with ``detail`` (the message), a stable ``code`` and any counts, kept as real JSON types."""

    def __init__(self, error):
        super().__init__(str(error))
        self.detail = {"detail": str(error), "code": error.code, **error.extra}


class PackageExpired(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "This generated package has expired."
    default_code = "package_expired"


class DownloadPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _enqueue_response(request, *, batch, package_type, report_format):
    try:
        package, created = enqueue_download_package(
            owner=request.user,
            batch=batch,
            package_type=package_type,
            report_format=report_format,
        )
    except PackageNotReadyError as error:
        # Same 409 and message as before, plus a stable code (and counts) a client can act on.
        raise PackageNotReadyWithReason(error) from error
    response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return Response(
        {
            "created": created,
            "package": DownloadPackageSerializer(
                package, context={"request": request}
            ).data,
        },
        status=response_status,
    )


class BatchPackageCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "download_package"

    @extend_schema(
        request=DownloadPackageRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, batch_id):
        batch = get_object_or_404(
            UploadBatch.objects.filter(owner=request.user), pk=batch_id
        )
        payload = DownloadPackageRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return _enqueue_response(
            request,
            batch=batch,
            package_type=payload.validated_data["package_type"],
            report_format=payload.validated_data["report_format"],
        )


class DownloadHistoryListView(generics.ListAPIView):
    serializer_class = DownloadEventSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = DownloadPagination

    def get_queryset(self):
        queryset = DownloadEvent.objects.filter(owner=self.request.user)
        resource_type = self.request.query_params.get("resource_type", "").strip()
        if resource_type:
            if resource_type not in DownloadEvent.ResourceType.values:
                raise ValidationError({"resource_type": "Unsupported resource type."})
            queryset = queryset.filter(resource_type=resource_type)
        batch_id = self.request.query_params.get("batch", "").strip()
        if batch_id:
            if not batch_id.isdigit():
                raise ValidationError({"batch": "Batch must be a positive integer."})
            queryset = queryset.filter(batch_id=int(batch_id))
        return queryset

    def paginate_queryset(self, queryset):
        page = super().paginate_queryset(queryset)
        self.extras = build_extras(self.request.user, page if page is not None else list(queryset))
        return page

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["download_extras"] = getattr(self, "extras", {})
        return context


class DownloadPackageListView(generics.ListAPIView):
    serializer_class = DownloadPackageSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = DownloadPagination

    def get_queryset(self):
        expire_due_packages(owner=self.request.user)
        queryset = DownloadPackage.objects.filter(owner=self.request.user).select_related("batch")
        filters = (
            ("status", DownloadPackage.Status.values),
            ("package_type", DownloadPackage.PackageType.values),
            ("report_format", DownloadPackage.ReportFormat.values),
        )
        for parameter, allowed_values in filters:
            value = self.request.query_params.get(parameter, "").strip()
            if not value:
                continue
            if value not in allowed_values:
                raise ValidationError({parameter: f"Unsupported {parameter} filter."})
            queryset = queryset.filter(**{parameter: value})
        batch_id = self.request.query_params.get("batch", "").strip()
        if batch_id:
            if not batch_id.isdigit():
                raise ValidationError({"batch": "Batch must be a positive integer."})
            queryset = queryset.filter(batch_id=int(batch_id))
        return queryset


class DownloadPackageDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = DownloadPackageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return DownloadPackage.objects.filter(owner=self.request.user).select_related("batch")

    def retrieve(self, request, *args, **kwargs):
        package = self.get_object()
        expire_package_if_needed(package)
        package.refresh_from_db()
        return Response(self.get_serializer(package).data)


class DownloadPackageFileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses={200: OpenApiTypes.BINARY, 409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        package = get_object_or_404(
            DownloadPackage.objects.filter(owner=request.user).select_related("batch"),
            pk=pk,
        )
        if expire_package_if_needed(package):
            raise PackageExpired(
                "This generated package has expired. Request regeneration to continue."
            )
        if package.status != DownloadPackage.Status.READY:
            raise PackageNotReady("The package file is not ready for download.")
        if not package.package_file or not package.package_file.storage.exists(
            package.package_file.name
        ):
            return Response(
                {"detail": "The generated package file is unavailable."},
                status=status.HTTP_410_GONE,
            )
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.BATCH_PACKAGE,
            resource_id=package.pk,
            batch_id=package.batch_id,
            filename=package.display_name,
            file_size=package.file_size,
            content_type="application/zip",
        )
        return FileResponse(
            package.package_file.open("rb"),
            as_attachment=True,
            filename=package.display_name,
            content_type="application/zip",
        )


class DownloadPackageRegenerateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "download_package"

    @extend_schema(
        request=None,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        package = get_object_or_404(
            DownloadPackage.objects.filter(owner=request.user).select_related("batch"),
            pk=pk,
        )
        expire_package_if_needed(package)
        package.refresh_from_db()
        if package.status not in {
            DownloadPackage.Status.FAILED,
            DownloadPackage.Status.EXPIRED,
        }:
            raise PackageNotReady(
                "Only failed or expired packages can be regenerated."
            )
        payload = DownloadPackageRequestSerializer(data={
            "package_type": package.package_type,
            "report_format": package.report_format,
        })
        payload.is_valid(raise_exception=True)
        return _enqueue_response(
            request,
            batch=package.batch,
            package_type=payload.validated_data["package_type"],
            report_format=payload.validated_data["report_format"],
        )

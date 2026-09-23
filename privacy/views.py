from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole
from organizations.permissions import OrganizationScopedMixin

from .models import OrganizationExport, PrivacyRequest
from .serializers import ConsentRequestSerializer, FacialDataErasureSerializer, OrganizationExportSerializer
from .services import (
    audit_report,
    build_privacy_export,
    consent_status,
    enqueue_organization_export,
    erase_facial_data,
    expire_organization_export_if_needed,
    facial_data_counts,
    record_consent,
    record_export_request,
)


class ConsentView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "privacy_consent"

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(consent_status(request.user))

    @extend_schema(request=ConsentRequestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = ConsentRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        record_consent(request.user, data["purpose"], data["granted"], data["policy_version"])
        response = consent_status(request.user)
        if data["erase_facial_data"]:
            response["erasure"] = erase_facial_data(
                request.user, trigger=PrivacyRequest.Trigger.CONSENT_WITHDRAWAL
            )
        return Response(response)


class PrivacyExportView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "privacy_export"

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        data = build_privacy_export(request.user)
        privacy_request = record_export_request(request.user)
        response = Response(data)
        response["Content-Disposition"] = (
            f'attachment; filename="facecloud-data-export-{privacy_request.pk}.json"'
        )
        return response


class FacialDataView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "privacy_erasure"

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response({"stored": facial_data_counts(request.user)})

    @extend_schema(request=FacialDataErasureSerializer, responses={200: OpenApiTypes.OBJECT})
    def delete(self, request):
        payload = FacialDataErasureSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(erase_facial_data(request.user))


class PrivacyAuditView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(audit_report())


class OrganizationExportNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The organization export is not ready for download."
    default_code = "export_not_ready"


class OrganizationExportExpired(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "This generated export has expired."
    default_code = "export_expired"


class OrganizationExportListCreateView(OrganizationScopedMixin, generics.ListAPIView):
    """Organization-wide data export (T-006), the org-scoped counterpart to
    the personal export above. See OrganizationExport's docstring for the
    scope boundary: organization-owned content only, never a member's
    personal account data.
    """

    require_owner_or_moderator = True
    serializer_class = OrganizationExportSerializer
    throttle_scope = "org_export"

    def get_queryset(self):
        return OrganizationExport.objects.filter(organization_id=self.kwargs["organization_id"])

    @extend_schema(
        request=None,
        responses={201: OrganizationExportSerializer, 200: OrganizationExportSerializer},
        summary="Request an organization-wide data export",
        description=(
            "Returns the existing in-flight export instead of creating a "
            "duplicate if one is already queued or generating."
        ),
    )
    def post(self, request, organization_id):
        export, created = enqueue_organization_export(
            organization=self.organization, requested_by=request.user,
        )
        return Response(
            OrganizationExportSerializer(export).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class OrganizationExportDetailView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(responses={200: OrganizationExportSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, organization_id, export_id):
        export = get_object_or_404(OrganizationExport, pk=export_id, organization_id=organization_id)
        expire_organization_export_if_needed(export)
        return Response(OrganizationExportSerializer(export).data)


class OrganizationExportFileView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(
        responses={200: OpenApiTypes.BINARY, 409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
        summary="Download a ready organization export",
    )
    def get(self, request, organization_id, export_id):
        export = get_object_or_404(OrganizationExport, pk=export_id, organization_id=organization_id)
        if expire_organization_export_if_needed(export):
            raise OrganizationExportExpired()
        if export.status != OrganizationExport.Status.READY:
            raise OrganizationExportNotReady()
        if not export.export_file or not export.export_file.storage.exists(export.export_file.name):
            return Response(
                {"detail": "The generated export file is unavailable."},
                status=status.HTTP_410_GONE,
            )
        return FileResponse(
            export.export_file.open("rb"), as_attachment=True,
            filename=f"facecloud-organization-export-{export.pk}.json",
            content_type="application/json",
        )

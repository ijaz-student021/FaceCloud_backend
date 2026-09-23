from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from .models import PrivacyRequest
from .serializers import ConsentRequestSerializer, FacialDataErasureSerializer
from .services import (
    audit_report,
    build_privacy_export,
    consent_status,
    erase_facial_data,
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

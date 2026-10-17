from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.serializers import UploadBatchSerializer
from uploads.services import create_image_batch

from .models import CameraSession, CameraSnapshot
from .serializers import CameraSessionDetailSerializer, CameraSessionSerializer, CameraSnapshotSerializer
from .services import purge_old_sessions


class CameraSessionListCreateView(generics.ListCreateAPIView):
    """The user's camera sessions from the last 30 days; POST starts one.

    A session records only metadata. No video is ever sent or stored.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CameraSessionSerializer

    def get_queryset(self):
        purge_old_sessions()
        return CameraSession.objects.filter(owner=self.request.user).order_by("-started_at")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class CameraSessionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: CameraSessionDetailSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, session_id):
        session = get_object_or_404(CameraSession, pk=session_id, owner=request.user)
        return Response(CameraSessionDetailSerializer(session).data)


class CameraSessionEndView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: CameraSessionSerializer, 404: OpenApiTypes.OBJECT})
    def post(self, request, session_id):
        session = get_object_or_404(CameraSession, pk=session_id, owner=request.user)
        if session.ended_at is None:
            session.ended_at = timezone.now()
            session.save(update_fields=["ended_at", "last_activity_at"])
        return Response(CameraSessionSerializer(session).data)


class CameraSnapshotCreateView(APIView):
    """Save one snapshot the user chose to take (an explicit action).

    The image becomes an ordinary single upload (same validation, limits,
    preprocessing and quality checks) linked to the session. 409 once the
    session has ended.
    """

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "image_upload"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}},
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT,
                   409: OpenApiTypes.OBJECT},
    )
    def post(self, request, session_id):
        session = get_object_or_404(CameraSession, pk=session_id, owner=request.user)
        if session.ended_at is not None:
            return Response({"detail": "This camera session has ended."}, status=status.HTTP_409_CONFLICT)
        files = request.FILES.getlist("file")
        if len(files) != 1:
            raise ValidationError({"file": "Send exactly one snapshot image."})
        batch = create_image_batch(request.user, "single", files)
        image = batch.images.get()
        snapshot = CameraSnapshot.objects.create(session=session, image=image)
        session.save(update_fields=["last_activity_at"])
        return Response({
            "snapshot": CameraSnapshotSerializer(snapshot).data,
            "batch": UploadBatchSerializer(batch, context={"request": request}).data,
        }, status=status.HTTP_201_CREATED)

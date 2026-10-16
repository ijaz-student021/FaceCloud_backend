from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Collage
from .serializers import CollageRequestSerializer, CollageSerializer
from .services import (
    CollageNotPossible,
    SourceNotFound,
    enqueue_collage,
    expire_collage_if_needed,
    expire_due_collages,
    select_collage_photos,
)


class CollageNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The collage is not ready."
    default_code = "collage_not_ready"


class CollageExpired(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "This collage has expired. Create a new one."
    default_code = "collage_expired"


class CollagePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class CollageListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CollagePagination

    def get_serializer_class(self):
        return CollageRequestSerializer if self.request.method == "POST" else CollageSerializer

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "collage"
        return super().get_throttles()

    def get_queryset(self):
        expire_due_collages(owner=self.request.user)
        return Collage.objects.filter(owner=self.request.user)

    @extend_schema(
        request=CollageRequestSerializer,
        responses={200: CollageSerializer, 202: CollageSerializer},
    )
    def post(self, request, *args, **kwargs):
        payload = CollageRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            image_ids, subject = select_collage_photos(
                request.user, data["source_type"], data["source_ref"], data.get("gap_hours"),
            )
        except SourceNotFound as error:
            raise NotFound(f"That {error} was not found.")
        except CollageNotPossible as error:
            body = {"detail": str(error)}
            if error.code:
                body["code"] = error.code
            raise ValidationError(body)
        collage, created = enqueue_collage(
            owner=request.user, source_type=data["source_type"], source_ref=data["source_ref"],
            image_ids=image_ids, title=data.get("title", ""), subject=subject,
        )
        return Response(
            CollageSerializer(collage, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class CollageDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = CollageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Collage.objects.filter(owner=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        collage = self.get_object()
        expire_collage_if_needed(collage)
        collage.refresh_from_db()
        return Response(self.get_serializer(collage).data)


class CollageFileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY, 409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        collage = get_object_or_404(Collage.objects.filter(owner=request.user), pk=pk)
        if expire_collage_if_needed(collage):
            raise CollageExpired()
        if collage.status != Collage.Status.READY:
            raise CollageNotReady()
        if not collage.collage_file or not collage.collage_file.storage.exists(collage.collage_file.name):
            return Response({"detail": "The collage file is unavailable."}, status=status.HTTP_410_GONE)
        return FileResponse(
            collage.collage_file.open("rb"), as_attachment=True,
            filename=f"collage-{collage.pk}.jpg", content_type="image/jpeg",
        )

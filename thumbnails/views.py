from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload

from .services import ThumbnailUnavailable, allowed_sizes, default_size, get_or_create_thumbnail


class ImageThumbnailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "thumbnail"

    @extend_schema(
        parameters=[OpenApiParameter(
            "size", int, required=False,
            description="Longest edge in pixels. Must be one of the configured sizes (default 256, also 1024).",
        )],
        responses={200: OpenApiTypes.BINARY, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        raw_size = request.query_params.get("size", "")
        size = default_size()
        if raw_size != "":
            if not raw_size.isdigit() or int(raw_size) not in allowed_sizes():
                raise ValidationError({"size": f"Choose one of: {', '.join(str(s) for s in allowed_sizes())}."})
            size = int(raw_size)
        image = get_object_or_404(ImageUpload.objects.filter(owner=request.user, status="uploaded"), pk=pk)
        try:
            thumbnail = get_or_create_thumbnail(image, size)
        except ThumbnailUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(thumbnail.file.open("rb"), content_type="image/jpeg")
        # A photo never changes after upload, so a thumbnail can be reused by this browser.
        response["Cache-Control"] = "private, max-age=3600"
        return response

from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload

from .services import VERSIONS, ThumbnailUnavailable, allowed_sizes, default_size, enhanced_result, get_or_create_thumbnail, render_thumbnail


class ImageThumbnailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "thumbnail"

    def images(self, request):
        """The photographs this endpoint may serve."""
        return ImageUpload.objects.filter(owner=request.user, status__in=("uploaded", "rejected"))     # a rejected photograph can still be looked at (never downloaded or enhanced)

    @extend_schema(
        parameters=[OpenApiParameter(
            "size", int, required=False,
            description="Longest edge in pixels. Must be one of the configured sizes (default 256, also 1024).",
        ), OpenApiParameter(
            "version", str, required=False, enum=list(VERSIONS),
            description=("`original` (the default: the photograph as uploaded) or `enhanced` (the picture of its latest finished enhancement; the original when there is none or the "
                         "enhancement kept the original). The header `X-Thumbnail-Version` says which one came back."),
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
        version = request.query_params.get("version", "original")
        if version not in VERSIONS:
            raise ValidationError({"version": f"Choose one of: {', '.join(VERSIONS)}."})
        image = get_object_or_404(self.images(request), pk=pk)
        result = enhanced_result(image) if version == "enhanced" else None
        if result is not None:
            return self.enhanced_response(request, result, size)
        try:
            thumbnail = get_or_create_thumbnail(image, size)
        except ThumbnailUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(thumbnail.file.open("rb"), content_type="image/jpeg")
        # A photo never changes after upload, so a thumbnail can be reused by this browser.
        response["Cache-Control"] = "private, max-age=3600"
        response["X-Thumbnail-Version"] = "original"
        return response

    def enhanced_response(self, request, result, size):
        """The enhanced picture at ``size``, rendered from the stored enhanced file. The tag names the result and the size, so a browser revalidates for a few bytes and a newer
        enhancement is shown at once (an enhanced picture, unlike the original, can be replaced)."""
        tag = f'W/"enhanced-{result.pk}-{size}"'
        if request.headers.get("If-None-Match") == tag:
            response = HttpResponse(status=status.HTTP_304_NOT_MODIFIED)
        else:
            try:
                payload, _width, _height = render_thumbnail(result.enhanced_file, size)
            except ThumbnailUnavailable as error:
                return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
            response = HttpResponse(payload, content_type="image/jpeg")
        response["ETag"] = tag
        response["Cache-Control"] = "private, no-cache"
        response["X-Thumbnail-Version"] = "enhanced"
        return response


class TrashThumbnailView(ImageThumbnailView):
    """The thumbnail of one of your photographs **in the trash** (the ordinary thumbnail answers 404 for it), so the trash can show what it holds."""

    def images(self, request):
        return ImageUpload.all_objects.in_trash().filter(owner=request.user, status__in=("uploaded", "rejected"))

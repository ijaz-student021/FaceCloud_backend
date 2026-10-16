from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.http import content_disposition_header
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from enhancement.watermark import WatermarkError, render_watermarked, validate_spec

from .models import ShareLink
from .serializers import (
    ShareLinkCreatedSerializer,
    ShareLinkCreateSerializer,
    ShareLinkDetailSerializer,
    ShareLinkSerializer,
)
from .services import (
    LinkGone,
    create_share_link,
    record_access,
    resolve_public_link,
    revoke_share_link,
    shared_file,
)

def _content_type(output_format):
    return {"png": "image/png", "zip": "application/zip"}.get(output_format, "image/jpeg")


PREVIEW_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


class ShareLinkListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "share_create"

    def get_serializer_class(self):
        return ShareLinkCreateSerializer if self.request.method == "POST" else ShareLinkSerializer

    def get_queryset(self):
        return ShareLink.objects.filter(owner=self.request.user)

    @extend_schema(
        summary="Create a share link",
        description=(
            "Returns the raw token **once**; only its hash is stored, so it cannot be "
            "shown again. Give `public_url` to the recipient."
        ),
        request=ShareLinkCreateSerializer,
        responses={
            201: ShareLinkCreatedSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample(
                "24-hour link with a username watermark",
                value={"result": 42, "ttl_hours": 24, "label": "For Sara", "watermark": "username"},
                request_only=True,
            ),
        ],
    )
    def post(self, request, *args, **kwargs):
        serializer = ShareLinkCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        spec = data["spec"]
        link, token = create_share_link(
            owner=request.user, result=data["result_object"], edit=data["edit_object"],
            ttl_hours=data.get("ttl_hours"), label=data.get("label", ""),
            watermark=spec.as_dict() if spec.active else None,
        )
        link.token = token
        return Response(
            ShareLinkCreatedSerializer(link, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ShareLinkDetailView(generics.RetrieveAPIView):
    serializer_class = ShareLinkDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ShareLink.objects.filter(owner=self.request.user)


class ShareLinkRevokeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=None, responses={200: ShareLinkSerializer, 404: OpenApiTypes.OBJECT},
        summary="Revoke a share link immediately",
    )
    def post(self, request, pk):
        link = get_object_or_404(ShareLink.objects.filter(owner=request.user), pk=pk)
        revoke_share_link(link)
        return Response(ShareLinkSerializer(link, context={"request": request}).data)


class PublicShareInfoView(APIView):
    """Metadata for a shared image, for a frontend page to show before loading it."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "share_public"

    @extend_schema(
        responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
        summary="Describe a shared image",
    )
    def get(self, request, token):
        link = resolve_public_link(token)
        file_field, output_format, filename, width, height = shared_file(link)
        if not file_field:
            raise LinkGone("The shared image is unavailable.")
        response = Response({
            "filename": filename,
            "content_type": _content_type(output_format),
            "width": width,
            "height": height,
            "watermarked": bool(link.watermark),
            "expires_at": link.expires_at,
        })
        for name, value in PREVIEW_HEADERS.items():
            response[name] = value
        return response


class PublicShareView(APIView):
    """Shows the shared image inline. No login; the unguessable token is the credential."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "share_public"

    @extend_schema(
        responses={
            200: OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT,
        },
        summary="View a shared image (or download a shared person-folder ZIP)",
        description=(
            "404 if the token is unknown, 410 if the link expired, was revoked, or its "
            "owner is no longer active. A link made from a person-folder export serves the ZIP "
            "as an attachment."
        ),
    )
    def get(self, request, token):
        link = resolve_public_link(token)
        file_field, output_format, filename, _, _ = shared_file(link)
        if not file_field:
            raise LinkGone("The shared image is unavailable.")
        content_type = _content_type(output_format)
        if link.identity_export_id:
            # A person folder's ZIP: downloaded, never watermarked or shown inline.
            try:
                response = FileResponse(file_field.open("rb"), content_type=content_type)
            except OSError as error:
                raise LinkGone("The shared download is unavailable.") from error
            response["Content-Disposition"] = content_disposition_header(True, filename)
            for name, value in PREVIEW_HEADERS.items():
                response[name] = value
            record_access(link, request.META.get("HTTP_USER_AGENT", ""))
            return response
        try:
            spec = validate_spec(**link.watermark) if link.watermark else None
            if spec and spec.active:
                payload, content_type = render_watermarked(
                    file_field, output_format, spec, link.owner,
                )
                response = HttpResponse(payload, content_type=content_type)
            else:
                response = FileResponse(file_field.open("rb"), content_type=content_type)
        except (WatermarkError, OSError) as error:
            raise LinkGone("The shared image is unavailable.") from error
        response["Content-Disposition"] = content_disposition_header(False, filename)
        for name, value in PREVIEW_HEADERS.items():
            response[name] = value
        record_access(link, request.META.get("HTTP_USER_AGENT", ""))
        return response

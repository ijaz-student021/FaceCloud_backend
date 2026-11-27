"""The endpoints of download approval (see ``downloads/approval.py``): the member's side under ``/api/downloads/requests/`` and the moderator's under
``/api/organizations/<id>/download-requests/``."""
import tempfile
import zipfile

from django.conf import settings
from django.db.models import Prefetch
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from enhancement.models import EnhancementStatus, ImageEditVersion
from organizations.models import OrganizationAuditEvent
from organizations.permissions import OrganizationScopedMixin
from organizations.services import record_audit_event

from . import approval
from .archives import _unique_archive_path
from .models import DownloadEvent, DownloadGrant, DownloadRequest, DownloadRequestItem
from .services import record_download
from .approval_serializers import (
    DownloadDecisionSerializer,
    DownloadGrantSerializer,
    DownloadRequestCreatedSerializer,
    DownloadRequestCreateSerializer,
    DownloadRequestDetailSerializer,
    DownloadRequestSerializer,
    EligibilityRequestSerializer,
    EligibilityResponseSerializer,
    GrantRevokeSerializer,
)

Item = DownloadRequestItem
ERROR_RESPONSES = {400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT}


def _with_items(queryset):
    return queryset.select_related("organization", "requester").prefetch_related(
        Prefetch("items", queryset=Item.objects.select_related("image", "grant").order_by("pk")),
    )


def _entries(validated_items):
    return [{"image_id": row["image_id"], "artifact_type": row.get("artifact_type"), "artifact_id": row.get("artifact_id")} for row in validated_items]


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# The member's side
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


class DownloadEligibilityView(APIView):
    """Which of these images can go into a download request now, and the download-access state of each."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=EligibilityRequestSerializer, responses={200: EligibilityResponseSerializer, **ERROR_RESPONSES}, summary="Check which images can be requested")
    def post(self, request):
        from organizations.services import get_active_membership

        payload = EligibilityRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = get_active_membership(request.user, payload.validated_data["organization_id"])
        if membership is None:
            raise ValidationError({"organization_id": ["You are not an active member of that organization."]})
        organization = membership.organization
        entries = _entries(payload.validated_data["items"])
        if len(entries) > settings.DOWNLOAD_REQUEST_MAX_ITEMS:
            raise ValidationError({"items": [f"Check at most {settings.DOWNLOAD_REQUEST_MAX_ITEMS} images at a time."]})
        return Response({
            "approval_required": approval.organization_requires_approval(organization),
            "reviewer_available": bool(approval.eligible_reviewers(organization, request.user.pk)),
            "items": approval.eligibility_report(request.user, organization, entries),
        })


class DownloadRequestListCreateView(generics.ListCreateAPIView):
    """POST: submit one request for several images of an organization that requires approval. GET: your own requests, newest first."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DownloadRequestSerializer

    @property
    def throttle_scope(self):
        return "download_package" if self.request.method == "POST" else None

    def get_queryset(self):
        queryset = _with_items(DownloadRequest.objects.filter(requester=self.request.user))
        wanted = self.request.query_params.get("status")
        if wanted:
            queryset = queryset.filter(status=wanted)
        organization = self.request.query_params.get("organization")
        if organization and organization.isdigit():
            queryset = queryset.filter(organization_id=int(organization))
        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter("status", str, enum=DownloadRequest.Status.values, description="Only requests with this status."),
            OpenApiParameter("organization", int, description="Only requests to this organization."),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        request=DownloadRequestCreateSerializer, responses={201: DownloadRequestCreatedSerializer, 200: DownloadRequestCreatedSerializer, **ERROR_RESPONSES},
        summary="Request permission to download several images",
        description=(
            "400 `items_not_eligible` lists every image that cannot be requested (nothing is saved then); 409 `approval_not_required` when the organization does not use approval; "
            "200 instead of 201 when `client_token` names a request that already exists."
        ),
    )
    def post(self, request, *args, **kwargs):
        payload = DownloadRequestCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        download_request, created = approval.create_request(
            user=request.user, organization_id=data["organization_id"], entries=_entries(data["items"]), message=data["message"], client_token=data["client_token"], http_request=request,
        )
        body = DownloadRequestDetailSerializer(_with_items(DownloadRequest.objects.filter(pk=download_request.pk)).get(), context={"for_requester": True}).data
        return Response({"created": created, "request": body}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class DownloadRequestDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: DownloadRequestDetailSerializer, **ERROR_RESPONSES})
    def get(self, request, pk):
        download_request = get_object_or_404(_with_items(DownloadRequest.objects.filter(requester=request.user)), pk=pk)
        return Response(DownloadRequestDetailSerializer(download_request, context={"for_requester": True}).data)


class DownloadRequestCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: DownloadRequestDetailSerializer, **ERROR_RESPONSES}, summary="Cancel a request nobody has started to decide")
    def post(self, request, pk):
        download_request = get_object_or_404(DownloadRequest.objects.filter(requester=request.user), pk=pk)
        approval.cancel_request(download_request=download_request, user=request.user, http_request=request)
        return Response(DownloadRequestDetailSerializer(_with_items(DownloadRequest.objects.filter(pk=pk)).get(), context={"for_requester": True}).data)


def _artifact(item):
    """``(file field, file name, content type, size, resource type)`` of what an approved item names, or ``None`` when it is gone."""
    image = item.image
    if item.artifact_type == Item.ArtifactType.ORIGINAL:
        return (image.original_file, image.original_name, image.mime_type or "application/octet-stream", image.file_size, DownloadEvent.ResourceType.ORIGINAL_IMAGE) if image.original_file else None
    if item.artifact_type == Item.ArtifactType.ENHANCED:
        result = image.enhancement_results.filter(pk=item.artifact_id, status=EnhancementStatus.COMPLETED).exclude(enhanced_file="").first()
        if result is None:
            return None
        return (result.enhanced_file, result.output_name or f"enhanced-{image.pk}", "image/png" if result.output_format == "png" else "image/jpeg", result.output_file_size, DownloadEvent.ResourceType.ENHANCED_IMAGE)
    edit = ImageEditVersion.objects.filter(pk=item.artifact_id, enhancement_result__image=image, status=EnhancementStatus.COMPLETED).exclude(edited_file="").first()
    if edit is None:
        return None
    return (edit.edited_file, edit.output_name or f"edited-{image.pk}", "image/png" if edit.output_format == "png" else "image/jpeg", edit.output_file_size, DownloadEvent.ResourceType.EDITED_IMAGE)


def _approved_item(request, pk, item_id):
    return get_object_or_404(
        Item.objects.select_related("request", "image__batch__organization", "image__quality_decision"),
        pk=item_id, request_id=pk, request__requester=request.user, status=Item.Status.APPROVED,
    )


class DownloadRequestItemFileView(APIView):
    """Download one approved image. The grant is checked on every call: once it expired, was revoked, or the image was rejected, this answers 403."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY, **ERROR_RESPONSES}, summary="Download one approved image")
    def get(self, request, pk, item_id):
        item = _approved_item(request, pk, item_id)
        found = _artifact(item)
        if found is None:
            return Response({"detail": "The stored image is unavailable.", "code": "artifact_unavailable"}, status=status.HTTP_404_NOT_FOUND)
        file_field, filename, content_type, size, resource_type = found
        approval.authorize_download(request.user, item.image, item.artifact_type, item.artifact_id, http_request=request)
        record_download(owner=request.user, resource_type=resource_type, resource_id=item.artifact_id, batch_id=item.image.batch_id, image_id=item.image_id, filename=filename, file_size=size, content_type=content_type)
        return FileResponse(file_field.open("rb"), as_attachment=True, filename=filename, content_type=content_type)


class DownloadRequestZipView(APIView):
    """A ZIP of every image of the request that is approved AND still authorized now. Denied, expired, revoked or no-longer-accepted images are left out."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "download_package"

    @extend_schema(responses={(200, "application/zip"): OpenApiTypes.BINARY, **ERROR_RESPONSES}, summary="Download the approved images of a request as a ZIP")
    def get(self, request, pk):
        download_request = get_object_or_404(DownloadRequest.objects.filter(requester=request.user), pk=pk)
        items = list(Item.objects.select_related("image__batch__organization", "image__quality_decision").filter(request=download_request, status=Item.Status.APPROVED))
        if not items:
            return Response({"detail": "No image of this request has been approved.", "code": "nothing_approved"}, status=status.HTTP_409_CONFLICT)
        archive_file = tempfile.TemporaryFile()                                           # removed when the response closes it
        used, included, first_error = set(), [], None
        try:
            with zipfile.ZipFile(archive_file, "w", zipfile.ZIP_STORED) as archive:
                for item in items:
                    found = _artifact(item)
                    if found is None:
                        continue
                    file_field, filename, _content_type, size, resource_type = found
                    try:
                        approval.authorize_download(request.user, item.image, item.artifact_type, item.artifact_id, http_request=request, record=False)
                    except approval.DownloadNotAuthorized as error:
                        first_error = first_error or error
                        continue
                    path = _unique_archive_path("images", filename, used)
                    with file_field.open("rb") as source, archive.open(path, "w") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                    included.append((item, resource_type, filename, size))
            if not included:
                archive_file.close()
                if first_error is not None:
                    raise first_error
                return Response({"detail": "The approved images are unavailable.", "code": "artifact_unavailable"}, status=status.HTTP_404_NOT_FOUND)
        except BaseException:
            archive_file.close()
            raise
        organization = download_request.organization
        record_audit_event(
            organization, request.user, OrganizationAuditEvent.Action.DOWNLOAD_COMPLETED, target_type="DownloadRequest", target_id=download_request.pk,
            new_value={"images": len(included), "image_ids": [item.image_id for item, _, _, _ in included], "as": "zip"}, request=request,
        )
        name = f"facecloud-download-request-{download_request.pk}.zip"
        archive_file.seek(0, 2)
        size = archive_file.tell()
        archive_file.seek(0)
        record_download(owner=request.user, resource_type=DownloadEvent.ResourceType.APPROVED_REQUEST_ZIP, resource_id=download_request.pk, filename=name, file_size=size, content_type="application/zip")
        return FileResponse(archive_file, as_attachment=True, filename=name, content_type="application/zip")


# --------------------------------------------------------------------------------------------------------------------------------------------------------------
# The moderator's side (owner or moderator of the organization; a moderator sees the requests of the members they may see)
# --------------------------------------------------------------------------------------------------------------------------------------------------------------


class OrganizationDownloadRequestMixin(OrganizationScopedMixin):
    require_owner_or_moderator = True

    def visible_requests(self):
        queryset = DownloadRequest.objects.filter(organization=self.organization)
        return self.data_scope.exclude(queryset, owner_field="requester_id")


class OrganizationDownloadRequestListView(OrganizationDownloadRequestMixin, generics.ListAPIView):
    """The download requests of this organization (owner or moderator), newest first. Not the requests of members another moderator invited, when the organization limits moderators to their own."""

    serializer_class = DownloadRequestSerializer

    def get_queryset(self):
        queryset = _with_items(self.visible_requests())
        wanted = self.request.query_params.get("status")
        if wanted:
            queryset = queryset.filter(status__in=[value for value in wanted.split(",") if value in DownloadRequest.Status.values])
        requester = self.request.query_params.get("requester")
        if requester and requester.isdigit():
            queryset = queryset.filter(requester_id=int(requester))
        return queryset

    @extend_schema(parameters=[
        OpenApiParameter("status", str, description="One status or several separated by commas: pending, in_review, approved, partially_approved, denied, cancelled."),
        OpenApiParameter("requester", int, description="Only requests of this person (user id)."),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class OrganizationDownloadRequestDetailView(OrganizationDownloadRequestMixin, APIView):
    """One request with all its images. Preview each image with the organization's image preview endpoint (`/api/organizations/{id}/moderator/images/{image_id}/preview/`)."""

    @extend_schema(responses={200: DownloadRequestDetailSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id, request_id):
        download_request = get_object_or_404(_with_items(self.visible_requests()), pk=request_id)
        return Response(DownloadRequestDetailSerializer(download_request).data)


class OrganizationDownloadRequestDecisionView(OrganizationDownloadRequestMixin, APIView):
    """Approve or deny the images of a request. The first decision on an image wins: a second moderator who decides the same image gets 409."""

    throttle_scope = "download_review"                # its own limit (THROTTLE_RATE_DOWNLOAD_REVIEW, 120/hour per moderator), not the invitation limit

    @extend_schema(
        request=DownloadDecisionSerializer, responses={200: DownloadRequestDetailSerializer, **ERROR_RESPONSES}, summary="Decide a download request",
        description="Approving creates one time-limited grant per approved image. The requester is told once, when every image is decided. You cannot decide your own request.",
    )
    def post(self, request, organization_id, request_id):
        payload = DownloadDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        reason = data.get("reason")
        approval.decide(
            download_request_id=request_id, actor=request.user, organization=self.organization, scope=self.data_scope, action=data["action"], item_ids=data.get("item_ids"),
            reason={"code": reason["code"], "text": reason.get("text", "")} if reason else None,
            item_reasons={key: {"code": value["code"], "text": value.get("text", "")} for key, value in (data.get("item_reasons") or {}).items()},
            expected_version=data.get("expected_version"), http_request=request,
        )
        return Response(DownloadRequestDetailSerializer(_with_items(DownloadRequest.objects.filter(pk=request_id)).get()).data)


class OrganizationDownloadGrantRevokeView(OrganizationDownloadRequestMixin, APIView):
    """Withdraw an approval before it expires: nothing further is delivered. A file already downloaded cannot be recalled."""

    throttle_scope = "download_review"

    @extend_schema(request=GrantRevokeSerializer, responses={200: DownloadGrantSerializer, **ERROR_RESPONSES})
    def post(self, request, organization_id, grant_id):
        payload = GrantRevokeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        grant = get_object_or_404(DownloadGrant.objects.filter(organization=self.organization), pk=grant_id)
        if not self.data_scope.sees(grant.grantee_id):
            from django.http import Http404

            raise Http404("Grant not found.")
        grant = approval.revoke_grant(grant=grant, actor=request.user, reason=payload.validated_data["reason"], http_request=request)
        return Response(DownloadGrantSerializer(grant).data)

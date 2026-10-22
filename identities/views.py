from django.db.models import Exists, F, OuterRef, Prefetch, Subquery
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload
from uploads.views import MyImagesPagination, my_images_queryset
from downloads.models import DownloadEvent
from downloads.services import record_download

from sharing.serializers import ShareLinkCreatedSerializer
from sharing.services import create_export_share_link

from .export_services import (IdentityExportNotReadyError,
                              enqueue_identity_group_export,
                              expire_due_identity_exports,
                              expire_identity_export_if_needed)
from .management import (delete_empty_group, merge_groups, move_membership,
                         remove_membership, rename_group, set_profile_membership)
from .models import (IdentityAnalysis, IdentityChange, IdentityGroup,
                     IdentityGroupExport, IdentityMembership)
from .avatars import AVATAR_SIZES, DEFAULT_AVATAR_SIZE, AvatarUnavailable, best_face, render_avatar
from .image_serializers import IdentityGroupImageSerializer
from .people import DEFAULT_LIMIT, MAX_LIMIT, people_queryset
from .serializers import (PersonChipSerializer, IdentityAnalysisSerializer, IdentityExportShareRequestSerializer, IdentityAnalyzeRequestSerializer,
                          IdentityChangeSerializer,
                          IdentityGroupExportRequestSerializer,
                          IdentityGroupExportSerializer, IdentityGroupSerializer,
                          IdentityMembershipSerializer, IdentityMergeSerializer,
                          IdentityMoveSerializer, IdentityProfileSerializer,
                          IdentityReasonSerializer, IdentityRenameSerializer,
                          IdentityReviewRequestSerializer,
                          IdentitySearchResponseSerializer, SuggestedMergeSerializer)
from .services import (enqueue_identity_analysis, group_member_photos,
                       review_membership, search_by_face)
from .suggestions import suggested_merges


class IdentityExportNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The requested identity export is not ready."
    default_code = "identity_export_not_ready"


class IdentityExportExpired(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "This identity export has expired."
    default_code = "identity_export_expired"


class IdentityImageAnalysisView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: IdentityAnalysisSerializer})
    def get(self, request, image_id):
        analysis = get_object_or_404(IdentityAnalysis.objects.filter(owner=request.user), image_id=image_id)
        return Response(IdentityAnalysisSerializer(analysis).data)

    @extend_schema(
        request=IdentityAnalyzeRequestSerializer,
        responses={202: IdentityAnalysisSerializer},
    )
    def post(self, request, image_id):
        image = get_object_or_404(ImageUpload.objects.filter(owner=request.user, status="uploaded"), pk=image_id)
        payload = IdentityAnalyzeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        analysis = enqueue_identity_analysis(image, force=payload.validated_data["force"])
        return Response(IdentityAnalysisSerializer(analysis).data, status=status.HTTP_202_ACCEPTED)


class IdentityVisualSearchView(APIView):
    """Feature 50: search the owner's own photos by uploading a face photo."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "identity_search"

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "query_image": {"type": "string", "format": "binary"},
        }}},
        responses={200: IdentitySearchResponseSerializer, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        uploaded = request.FILES.get("query_image")
        if uploaded is None:
            raise ValidationError({"query_image": "Select a photo to search with."})
        result = search_by_face(request.user, uploaded)
        matches = []
        for item in result["matches"]:
            photos = list(group_member_photos(item["group"]))
            matches.append({
                "group_id": item["group"].pk,
                "display_name": item["group"].display_name,
                "similarity": round(item["similarity"], 4),
                "photo_count": len(photos),
                "photos": photos,
            })
        payload = {"faces_detected": result["faces_detected"], "matches": matches}
        return Response(IdentitySearchResponseSerializer(payload, context={"request": request}).data)


def _member_prefetch():
    """Each group's members (with their photo) in one extra query for a whole page of groups."""
    return Prefetch(
        "memberships",
        queryset=IdentityMembership.objects.exclude(
            state__in=[IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED],
        ).select_related("identity_face__image"),
        to_attr="active_memberships",
    )


class IdentityGroupListView(generics.ListAPIView):
    serializer_class = IdentityGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return IdentityGroup.objects.filter(
            owner=self.request.user, state=IdentityGroup.State.ACTIVE,
        ).prefetch_related(_member_prefetch())


class IdentityPeopleView(APIView):
    """The people of the library as chips (a face, a name, a count), most photographs first: what a search bar offers before anything is typed."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        operation_id="identities_people_list",
        parameters=[
            OpenApiParameter("q", str, required=False, description="Names containing this text (case-insensitive)."),
            OpenApiParameter("limit", int, required=False, description=f"How many people; default {DEFAULT_LIMIT}, at most {MAX_LIMIT}."),
            OpenApiParameter("include_unnamed", bool, required=False, description="false hides folders that still have their automatic name; default true."),
        ],
        responses={200: PersonChipSerializer(many=True), 400: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        raw = request.query_params.get("limit", "")
        if raw and (not raw.isdigit() or int(raw) < 1):
            raise ValidationError({"limit": "Limit must be a positive whole number."})
        limit = min(int(raw), MAX_LIMIT) if raw else DEFAULT_LIMIT
        unnamed = request.query_params.get("include_unnamed", "true").strip().lower()
        if unnamed not in {"true", "false", "1", "0"}:
            raise ValidationError({"include_unnamed": "Use true or false."})
        people = people_queryset(request.user, query=request.query_params.get("q", "").strip(), include_unnamed=unnamed in {"true", "1"})[:limit]
        return Response(PersonChipSerializer(people, many=True, context={"request": request}).data)


class IdentityGroupAvatarView(APIView):
    """A square crop of the best face of one person folder, as a JPEG (``?size=96|192|384``)."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        operation_id="identities_groups_avatar_retrieve",
        parameters=[OpenApiParameter("size", int, required=False, description="96 (default), 192 or 384 pixels.")],
        responses={(200, "image/jpeg"): OpenApiTypes.BINARY, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        group = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=pk)
        raw = request.query_params.get("size", "")
        if raw and (not raw.isdigit() or int(raw) not in AVATAR_SIZES):
            raise ValidationError({"size": f"Use one of {', '.join(str(size) for size in AVATAR_SIZES)}."})
        size = int(raw) if raw else DEFAULT_AVATAR_SIZE
        face = best_face(group)
        if face is None:
            raise Http404("This person has no face to show yet.")
        try:
            payload = render_avatar(face, size)
        except AvatarUnavailable as error:
            raise Http404(str(error)) from error
        response = HttpResponse(payload, content_type="image/jpeg")
        response["Cache-Control"] = "private, max-age=3600"
        return response


class IdentityGroupDetailView(generics.RetrieveAPIView):
    serializer_class = IdentityGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return IdentityGroup.objects.filter(owner=self.request.user).prefetch_related(_member_prefetch())

    def retrieve(self, request, *args, **kwargs):
        group = self.get_object()
        data = IdentityGroupSerializer(group, context={"request": request}).data
        data["memberships"] = IdentityMembershipSerializer(group.active_memberships, many=True).data
        return Response(data)


class IdentityGroupImagesView(generics.ListAPIView):
    """The photos in one person folder, newest first, with the facts a folder grid shows."""

    serializer_class = IdentityGroupImageSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = MyImagesPagination

    def get_queryset(self):
        group = get_object_or_404(IdentityGroup.objects.filter(owner=self.request.user), pk=self.kwargs["pk"])
        members = IdentityMembership.objects.filter(
            owner=self.request.user, identity_group=group, identity_face__image=OuterRef("pk"),
        ).exclude(state__in=[IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED])
        best = members.order_by(F("similarity_score").desc(nulls_last=True), "pk")
        return (
            my_images_queryset(self.request.user)
            .filter(Exists(members))
            .annotate(
                group_similarity=Subquery(best.values("similarity_score")[:1]),
                group_membership_id=Subquery(best.values("pk")[:1]),
                group_membership_state=Subquery(best.values("state")[:1]),
            )
            .order_by("-created_at", "-pk")
        )


class IdentityReviewListView(generics.ListAPIView):
    serializer_class = IdentityMembershipSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return IdentityMembership.objects.filter(owner=self.request.user, state=IdentityMembership.State.NEEDS_REVIEW).select_related("identity_face", "identity_group")


class IdentityAssignmentReviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityReviewRequestSerializer,
        responses={200: IdentityMembershipSerializer},
    )
    def patch(self, request, pk):
        membership = get_object_or_404(IdentityMembership.objects.filter(owner=request.user), pk=pk)
        payload = IdentityReviewRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = review_membership(membership, request.user, **payload.validated_data)
        return Response(IdentityMembershipSerializer(membership).data)


class IdentityGroupRenameView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityRenameSerializer,
        responses={200: IdentityGroupSerializer},
    )
    def patch(self, request, pk):
        group = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=pk)
        payload = IdentityRenameSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(IdentityGroupSerializer(rename_group(group, request.user, payload.validated_data["display_name"])).data)


class IdentitySuggestedMergesView(APIView):
    """The owner's own person folders that look like one person (nothing is merged for them)."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: SuggestedMergeSerializer(many=True)}, summary="Person folders that may be the same person")
    def get(self, request):
        return Response(suggested_merges(request.user))


class IdentityGroupMergeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityMergeSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        target = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=pk)
        payload = IdentityMergeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        source = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=payload.validated_data["source_group_id"])
        source, target = merge_groups(source, target, request.user, payload.validated_data["reason"])
        return Response({"source": IdentityGroupSerializer(source).data, "target": IdentityGroupSerializer(target).data})


class IdentityMembershipMoveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityMoveSerializer,
        responses={200: IdentityMembershipSerializer},
    )
    def patch(self, request, pk):
        membership = get_object_or_404(IdentityMembership.objects.filter(owner=request.user), pk=pk)
        payload = IdentityMoveSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        target = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=payload.validated_data["target_group_id"])
        return Response(IdentityMembershipSerializer(move_membership(membership, target, request.user, payload.validated_data["reason"])).data)


class IdentityMembershipRemoveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityReasonSerializer,
        responses={200: IdentityMembershipSerializer},
    )
    def patch(self, request, pk):
        membership = get_object_or_404(IdentityMembership.objects.filter(owner=request.user), pk=pk)
        payload = IdentityReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return Response(IdentityMembershipSerializer(remove_membership(membership, request.user, payload.validated_data["reason"])).data)


class IdentityGroupProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityProfileSerializer,
        responses={200: IdentityGroupSerializer},
    )
    def patch(self, request, pk):
        group = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=pk)
        payload = IdentityProfileSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = get_object_or_404(IdentityMembership.objects.filter(owner=request.user), pk=payload.validated_data["membership_id"])
        return Response(IdentityGroupSerializer(set_profile_membership(group, membership, request.user)).data)


class IdentityGroupDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=IdentityReasonSerializer,
        responses={204: None},
    )
    def delete(self, request, pk):
        group = get_object_or_404(IdentityGroup.objects.filter(owner=request.user), pk=pk)
        payload = IdentityReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        delete_empty_group(group, request.user, payload.validated_data["reason"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class IdentityChangeListView(generics.ListAPIView):
    serializer_class = IdentityChangeSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return IdentityChange.objects.filter(owner=self.request.user)


class IdentityGroupExportListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "download_package"

    def _group(self, request, group_id):
        return get_object_or_404(
            IdentityGroup.objects.filter(owner=request.user), pk=group_id
        )

    @extend_schema(responses={200: IdentityGroupExportSerializer(many=True)})
    def get(self, request, group_id):
        group = self._group(request, group_id)
        expire_due_identity_exports(owner=request.user)
        exports = IdentityGroupExport.objects.filter(
            owner=request.user, identity_group=group
        )
        return Response(
            IdentityGroupExportSerializer(
                exports, many=True, context={"request": request}
            ).data
        )

    @extend_schema(
        request=IdentityGroupExportRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, group_id):
        group = self._group(request, group_id)
        payload = IdentityGroupExportRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            export, created = enqueue_identity_group_export(
                owner=request.user,
                group=group,
                options=payload.validated_data,
            )
        except IdentityExportNotReadyError as error:
            raise IdentityExportNotReady(str(error)) from error
        response_status = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return Response(
            {
                "created": created,
                "export": IdentityGroupExportSerializer(
                    export, context={"request": request}
                ).data,
            },
            status=response_status,
        )


class IdentityGroupExportDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = IdentityGroupExportSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return IdentityGroupExport.objects.filter(
            owner=self.request.user
        ).select_related("identity_group")

    def retrieve(self, request, *args, **kwargs):
        export = self.get_object()
        expire_identity_export_if_needed(export)
        export.refresh_from_db()
        return Response(self.get_serializer(export).data)


class IdentityGroupExportDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses={200: OpenApiTypes.BINARY, 409: OpenApiTypes.OBJECT, 410: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        export = get_object_or_404(
            IdentityGroupExport.objects.filter(owner=request.user).select_related(
                "identity_group"
            ),
            pk=pk,
        )
        if expire_identity_export_if_needed(export):
            raise IdentityExportExpired(
                "This identity export has expired. Request regeneration to continue."
            )
        if export.status != IdentityGroupExport.Status.READY:
            raise IdentityExportNotReady("The identity export is not ready for download.")
        if not export.export_file or not export.export_file.storage.exists(
            export.export_file.name
        ):
            return Response(
                {"detail": "The generated identity export is unavailable."},
                status=status.HTTP_410_GONE,
            )
        record_download(
            owner=request.user,
            resource_type=DownloadEvent.ResourceType.IDENTITY_GROUP_PACKAGE,
            resource_id=export.pk,
            filename=export.display_name,
            file_size=export.file_size,
            content_type="application/zip",
        )
        return FileResponse(
            export.export_file.open("rb"),
            as_attachment=True,
            filename=export.display_name,
            content_type="application/zip",
        )


class IdentityGroupExportShareView(APIView):
    """A time-limited public link to a finished folder ZIP, made and revoked like an image share link."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "share_create"

    @extend_schema(
        summary="Share a finished person-folder ZIP",
        description=(
            "Returns the raw token **once**; only its hash is stored. Revoke with "
            "`POST /api/shares/{id}/revoke/`; the link also stops working when the ZIP expires. "
            "The recipient opens `public_url` (no login) and receives the ZIP as a download."
        ),
        request=IdentityExportShareRequestSerializer,
        responses={
            201: ShareLinkCreatedSerializer, 400: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT,
            410: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request, pk):
        export = get_object_or_404(IdentityGroupExport.objects.filter(owner=request.user), pk=pk)
        payload = IdentityExportShareRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        if expire_identity_export_if_needed(export):
            raise IdentityExportExpired("This identity export has expired. Request regeneration to continue.")
        if export.status != IdentityGroupExport.Status.READY:
            raise IdentityExportNotReady("Only a finished identity export can be shared.")
        if not export.export_file or not export.export_file.storage.exists(export.export_file.name):
            raise IdentityExportExpired("The generated identity export is unavailable.")
        link, token = create_export_share_link(
            owner=request.user, export=export,
            ttl_hours=payload.validated_data.get("ttl_hours"), label=payload.validated_data.get("label", ""),
        )
        link.token = token
        return Response(
            ShareLinkCreatedSerializer(link, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class IdentityGroupExportRegenerateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "download_package"

    @extend_schema(
        request=None,
        responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        export = get_object_or_404(
            IdentityGroupExport.objects.filter(owner=request.user).select_related(
                "identity_group"
            ),
            pk=pk,
        )
        expire_identity_export_if_needed(export)
        export.refresh_from_db()
        if export.status not in {
            IdentityGroupExport.Status.FAILED,
            IdentityGroupExport.Status.EXPIRED,
        }:
            raise IdentityExportNotReady(
                "Only failed or expired identity exports can be regenerated."
            )
        options = {
            "include_originals": export.include_originals,
            "include_preprocessed": export.include_preprocessed,
            "include_enhanced": export.include_enhanced,
            "include_report": export.include_report,
            "report_format": export.report_format,
            "image_ids": list(export.image_ids),
        }
        try:
            regenerated, created = enqueue_identity_group_export(
                owner=request.user,
                group=export.identity_group,
                options=options,
            )
        except IdentityExportNotReadyError as error:
            raise IdentityExportNotReady(str(error)) from error
        return Response(
            {
                "created": created,
                "export": IdentityGroupExportSerializer(
                    regenerated, context={"request": request}
                ).data,
            },
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )

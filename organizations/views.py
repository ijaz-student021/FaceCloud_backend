from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from .models import Organization, OrganizationInvitation, OrganizationMembership
from .permissions import OrganizationScopedMixin
from .scope import visible_audit_events
from .serializers import (
    AdminOrganizationSerializer,
    AdminOrganizationUpdateSerializer,
    OrganizationDetailSerializer,
    OrganizationWithMembershipSerializer,
    MemberRemovalSerializer,
    OrganizationAuditEventSerializer,
    OrganizationCreateSerializer,
    OrganizationInvitationAcceptSerializer,
    OrganizationInvitationPreviewSerializer,
    OrganizationInvitationCreateSerializer,
    OrganizationInvitationSerializer,
    OrganizationMembershipSerializer,
    OrganizationMembershipUpdateSerializer,
    OwnerOrganizationUpdateSerializer,
)
from .services import (
    accept_invitation,
    cancel_invitation,
    create_organization,
    get_active_membership,
    invite_member,
    preview_invitation,
    remove_member,
    resend_invitation,
    update_member,
    organizations_with_head_counts,
    update_organization,
)

ERROR_RESPONSES = {
    400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT,
    404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT,
}


class OrganizationListView(generics.ListAPIView):
    """Organizations the caller has an active membership in, each with the
    caller's role, status and capabilities there."""

    serializer_class = OrganizationWithMembershipSerializer

    def get_queryset(self):
        return Organization.objects.filter(
            memberships__user=self.request.user,
            memberships__status=OrganizationMembership.Status.ACTIVE,
        ).distinct()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if getattr(self.request, "user", None) and self.request.user.is_authenticated:
            context["memberships"] = {
                membership.organization_id: membership
                for membership in OrganizationMembership.objects.select_related("organization").filter(
                    user=self.request.user, status=OrganizationMembership.Status.ACTIVE,
                )
            }
        return context


class AdminOrganizationListCreateView(generics.ListCreateAPIView):
    """FaceCloud-administrator-only: create and list every organization."""

    permission_classes = [IsAdminRole]
    throttle_scope = "org_invite"

    def get_queryset(self):
        return organizations_with_head_counts()

    def get_serializer_class(self):
        return OrganizationCreateSerializer if self.request.method == "POST" else AdminOrganizationSerializer

    @extend_schema(request=OrganizationCreateSerializer, responses={201: AdminOrganizationSerializer, **ERROR_RESPONSES})
    def post(self, request, *args, **kwargs):
        serializer = OrganizationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = create_organization(
            name=serializer.validated_data["name"], owner=serializer.validated_data["owner"],
            plan=serializer.validated_data.get("plan", Organization.Plan.FREE),
            actor=request.user, request=request,
        )
        return Response(AdminOrganizationSerializer(organizations_with_head_counts().get(pk=organization.pk)).data, status=status.HTTP_201_CREATED)


class AdminOrganizationDetailView(APIView):
    """FaceCloud-administrator-only: read one organization, or rename it, change its plan, suspend or reactivate it."""

    permission_classes = [IsAdminRole]

    @extend_schema(responses={200: AdminOrganizationSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id):
        organization = get_object_or_404(organizations_with_head_counts(), pk=organization_id)
        return Response(AdminOrganizationSerializer(organization).data)

    @extend_schema(request=AdminOrganizationUpdateSerializer, responses={200: AdminOrganizationSerializer, **ERROR_RESPONSES})
    def patch(self, request, organization_id):
        organization = get_object_or_404(Organization, pk=organization_id)
        serializer = AdminOrganizationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        update_organization(
            organization=organization, actor=request.user, name=data.get("name"), plan=data.get("plan"), status=data.get("status"),
            moderator_data_scope=data.get("moderator_data_scope"), reason=data.get("reason", ""), request=request,
        )
        return Response(AdminOrganizationSerializer(organizations_with_head_counts().get(pk=organization.pk)).data)


class OrganizationDetailView(OrganizationScopedMixin, APIView):
    """One organization: for its owner and moderators also who owns it and how many people are in it. The owner can change its name and what its moderators see."""

    def _detail(self, request, organization_id):
        membership = get_active_membership(request.user, organization_id)                       # fresh: capabilities read the organization's current setting
        organization = organizations_with_head_counts().get(pk=organization_id)
        return OrganizationDetailSerializer(organization, context={"request": request, "memberships": {organization.pk: membership}}).data

    @extend_schema(responses={200: OrganizationDetailSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id):
        return Response(self._detail(request, organization_id))

    @extend_schema(request=OwnerOrganizationUpdateSerializer, responses={200: OrganizationDetailSerializer, **ERROR_RESPONSES})
    def patch(self, request, organization_id):
        if self.active_membership.role != OrganizationMembership.Role.OWNER:
            raise PermissionDenied("Only an organization owner can change these settings.")
        if self.organization.is_personal:
            raise PermissionDenied("A personal workspace has no settings to change.")
        if any(key in request.data for key in ("plan", "status")):
            raise PermissionDenied("The plan and the status are changed by the FaceCloud administrator.")
        serializer = OwnerOrganizationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        update_organization(
            organization=self.organization, actor=request.user, name=data.get("name"),
            moderator_data_scope=data.get("moderator_data_scope"), reason=data.get("reason", ""), request=request,
        )
        return Response(self._detail(request, organization_id))


class OrganizationMemberListView(OrganizationScopedMixin, generics.ListAPIView):
    require_owner_or_moderator = True
    serializer_class = OrganizationMembershipSerializer

    def get_queryset(self):
        return self.data_scope.exclude_memberships(
            OrganizationMembership.objects.filter(organization_id=self.kwargs["organization_id"])
            .exclude(status=OrganizationMembership.Status.REMOVED)
            .select_related("user", "invited_by")
        )


class OrganizationMemberDetailView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    def _get_membership(self):
        return get_object_or_404(
            self.data_scope.exclude_memberships(OrganizationMembership.objects.filter(organization_id=self.kwargs["organization_id"])),
            pk=self.kwargs["membership_id"],
        )

    @extend_schema(
        request=OrganizationMembershipUpdateSerializer,
        responses={200: OrganizationMembershipSerializer, **ERROR_RESPONSES},
        summary="Change a member's role or status",
    )
    def patch(self, request, organization_id, membership_id):
        serializer = OrganizationMembershipUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = update_member(
            membership=self._get_membership(), actor_membership=self.active_membership,
            role=serializer.validated_data.get("role"), status_value=serializer.validated_data.get("status"),
            reason=serializer.validated_data.get("reason", ""), request=request,
        )
        return Response(OrganizationMembershipSerializer(updated).data)

    @extend_schema(
        request=MemberRemovalSerializer,
        responses={200: OrganizationMembershipSerializer, **ERROR_RESPONSES},
        summary="Remove a member from the organization",
        description=(
            "Removes organization access only; it does not delete the member's "
            "global FaceCloud account."
        ),
    )
    def delete(self, request, organization_id, membership_id):
        serializer = MemberRemovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        removed = remove_member(
            membership=self._get_membership(), actor_membership=self.active_membership,
            reason=serializer.validated_data.get("reason", ""), request=request,
        )
        return Response(OrganizationMembershipSerializer(removed).data)


class OrganizationInvitationListCreateView(OrganizationScopedMixin, generics.ListCreateAPIView):
    require_owner_or_moderator = True
    throttle_scope = "org_invite"
    throttle_classes = [ScopedRateThrottle]

    def get_serializer_class(self):
        return (
            OrganizationInvitationCreateSerializer if self.request.method == "POST"
            else OrganizationInvitationSerializer
        )

    def get_queryset(self):
        return self.data_scope.exclude_invitations(
            OrganizationInvitation.objects.filter(organization_id=self.kwargs["organization_id"]).select_related("invited_by")
        )

    @extend_schema(
        request=OrganizationInvitationCreateSerializer,
        responses={201: OrganizationInvitationSerializer, **ERROR_RESPONSES},
        summary="Invite a member by email",
    )
    def post(self, request, organization_id):
        serializer = OrganizationInvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = invite_member(
            organization=self.organization, actor_membership=self.active_membership,
            email=serializer.validated_data["email"], role=serializer.validated_data["role"],
            request=request,
        )
        return Response(
            OrganizationInvitationSerializer(invitation).data, status=status.HTTP_201_CREATED,
        )


class OrganizationInvitationResendView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True
    throttle_scope = "org_invite"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        request=None, responses={200: OrganizationInvitationSerializer, **ERROR_RESPONSES},
        summary="Resend a pending invitation",
    )
    def post(self, request, organization_id, invitation_id):
        invitation = get_object_or_404(
            self.data_scope.exclude_invitations(OrganizationInvitation.objects.filter(organization_id=organization_id)), pk=invitation_id,
        )
        invitation = resend_invitation(
            invitation=invitation, actor_membership=self.active_membership, request=request,
        )
        return Response(OrganizationInvitationSerializer(invitation).data)


class OrganizationInvitationCancelView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    @extend_schema(
        request=None, responses={200: OrganizationInvitationSerializer, **ERROR_RESPONSES},
        summary="Cancel a pending invitation",
    )
    def delete(self, request, organization_id, invitation_id):
        invitation = get_object_or_404(
            self.data_scope.exclude_invitations(OrganizationInvitation.objects.filter(organization_id=organization_id)), pk=invitation_id,
        )
        invitation = cancel_invitation(
            invitation=invitation, actor_membership=self.active_membership, request=request,
        )
        return Response(OrganizationInvitationSerializer(invitation).data)


class OrganizationInvitationPreviewView(APIView):
    """Public: which organization an invitation is from, to which email and role, and when it expires.

    The link in the invitation email is ``{FRONTEND_URL}/invitations/{token}``; the page behind it calls this before asking the person to sign
    in. An unknown, used, cancelled or expired token all answer the same 404, so nothing is learned from a wrong guess.
    """

    authentication_classes = []                 # a stale sign-in on the browser must not turn a good token into a 401
    permission_classes = [permissions.AllowAny]
    throttle_scope = "invitation"

    @extend_schema(
        responses={200: OrganizationInvitationPreviewSerializer, 404: OpenApiTypes.OBJECT},
        summary="Preview an organization invitation",
        auth=[],
    )
    def get(self, request, token):
        invitation = preview_invitation(token)
        if invitation is None:
            return Response({"detail": "This invitation is invalid, used or expired."}, status=status.HTTP_404_NOT_FOUND)
        response = Response(OrganizationInvitationPreviewSerializer(invitation).data)
        response["Cache-Control"] = "no-store"
        return response


class OrganizationInvitationAcceptView(APIView):
    @extend_schema(
        request=OrganizationInvitationAcceptSerializer,
        responses={200: OrganizationMembershipSerializer, **ERROR_RESPONSES},
        summary="Accept an organization invitation",
    )
    def post(self, request):
        serializer = OrganizationInvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = accept_invitation(
            token=serializer.validated_data["token"], user=request.user, request=request,
        )
        return Response(OrganizationMembershipSerializer(membership).data)


class OrganizationAuditListView(OrganizationScopedMixin, generics.ListAPIView):
    """The organization's audit trail, newest first; ``?action=``, ``?actor=``, ``?q=``, ``?date_from=``, ``?date_to=``, ``?target_type=``, ``?target_id=``."""

    require_owner_or_moderator = True
    serializer_class = OrganizationAuditEventSerializer

    @extend_schema(parameters=[
        OpenApiParameter("action", OpenApiTypes.STR, description="One audit action, for example moderator_review_decided."),
        OpenApiParameter("actor", OpenApiTypes.INT, description="A user id."),
        OpenApiParameter("q", OpenApiTypes.STR, description="Text in the actor's name or email, the action (its name or sentence) or the reason; up to 100 characters."),
        OpenApiParameter("date_from", OpenApiTypes.DATE, description="ISO-8601."),
        OpenApiParameter("date_to", OpenApiTypes.DATE, description="ISO-8601; a bare date includes that whole day."),
        OpenApiParameter("target_type", OpenApiTypes.STR, description="For example ModeratorReview, UploadBatch, IdentityGroup."),
        OpenApiParameter("target_id", OpenApiTypes.INT),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        from .audit_reading import filter_events

        queryset = visible_audit_events(self.organization.audit_events.select_related("actor"), self.organization, self.data_scope)
        try:
            return filter_events(queryset, self.request.query_params)
        except ValueError as error:
            raise ValidationError({"detail": [str(error)]}) from error

    def list(self, request, *args, **kwargs):
        from .audit_reading import labels_for

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        events = list(page if page is not None else queryset)
        serializer = self.get_serializer(events, many=True, context={**self.get_serializer_context(), "labels": labels_for(events)})
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

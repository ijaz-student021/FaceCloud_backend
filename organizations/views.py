from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from .models import Organization, OrganizationInvitation, OrganizationMembership
from .permissions import OrganizationScopedMixin
from .serializers import (
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
    OrganizationSerializer,
)
from .services import (
    accept_invitation,
    cancel_invitation,
    create_organization,
    invite_member,
    preview_invitation,
    remove_member,
    resend_invitation,
    update_member,
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
                for membership in OrganizationMembership.objects.filter(
                    user=self.request.user, status=OrganizationMembership.Status.ACTIVE,
                )
            }
        return context


class AdminOrganizationListCreateView(generics.ListCreateAPIView):
    """FaceCloud-administrator-only: create and list every organization."""

    permission_classes = [IsAdminRole]
    queryset = Organization.objects.all()
    throttle_scope = "org_invite"

    def get_serializer_class(self):
        return OrganizationCreateSerializer if self.request.method == "POST" else OrganizationSerializer

    @extend_schema(request=OrganizationCreateSerializer, responses={201: OrganizationSerializer, **ERROR_RESPONSES})
    def post(self, request, *args, **kwargs):
        serializer = OrganizationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = create_organization(
            name=serializer.validated_data["name"], owner=serializer.validated_data["owner"],
            plan=serializer.validated_data.get("plan", Organization.Plan.FREE),
            actor=request.user, request=request,
        )
        return Response(OrganizationSerializer(organization).data, status=status.HTTP_201_CREATED)


class OrganizationDetailView(OrganizationScopedMixin, APIView):
    @extend_schema(responses={200: OrganizationWithMembershipSerializer, **ERROR_RESPONSES})
    def get(self, request, organization_id):
        context = {"request": request, "memberships": {self.organization.pk: self.active_membership}}
        return Response(OrganizationWithMembershipSerializer(self.organization, context=context).data)


class OrganizationMemberListView(OrganizationScopedMixin, generics.ListAPIView):
    require_owner_or_moderator = True
    serializer_class = OrganizationMembershipSerializer

    def get_queryset(self):
        return (
            OrganizationMembership.objects.filter(organization_id=self.kwargs["organization_id"])
            .exclude(status=OrganizationMembership.Status.REMOVED)
            .select_related("user", "invited_by")
        )


class OrganizationMemberDetailView(OrganizationScopedMixin, APIView):
    require_owner_or_moderator = True

    def _get_membership(self):
        return get_object_or_404(
            OrganizationMembership, pk=self.kwargs["membership_id"],
            organization_id=self.kwargs["organization_id"],
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
        return (
            OrganizationInvitation.objects.filter(organization_id=self.kwargs["organization_id"])
            .select_related("invited_by")
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
            OrganizationInvitation, pk=invitation_id, organization_id=organization_id,
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
            OrganizationInvitation, pk=invitation_id, organization_id=organization_id,
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
    require_owner_or_moderator = True
    serializer_class = OrganizationAuditEventSerializer

    def get_queryset(self):
        return self.organization.audit_events.select_related("actor")

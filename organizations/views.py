from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
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

        queryset = self.organization.audit_events.select_related("actor")
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

"""HTTP for the leave-request workflow (see organizations/leave.py and docs/features/organization-leave-request.md)."""
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from . import leave
from .models import Organization, OrganizationLeaveRequest
from .permissions import OrganizationScopedMixin
from .views import ERROR_RESPONSES


class LeaveMemberSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    full_name = serializers.CharField()
    email = serializers.EmailField()


class LeaveRequestSerializer(serializers.ModelSerializer):
    organization_id = serializers.IntegerField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    member = serializers.SerializerMethodField()
    reviewed_by = serializers.SerializerMethodField()
    membership_status = serializers.CharField(source="membership.status", read_only=True)

    class Meta:
        model = OrganizationLeaveRequest
        fields = [
            "id", "organization_id", "organization_name", "member", "status", "reason", "submitted_at", "reviewed_by", "decided_at", "decision_reason",
            "admin_override", "membership_status",
        ]
        read_only_fields = fields

    def get_member(self, leave_request) -> dict:
        user = leave_request.user
        return {"id": user.pk, "username": user.username, "full_name": user.full_name, "email": user.email}

    def get_reviewed_by(self, leave_request) -> dict | None:
        reviewer = leave_request.reviewed_by
        return None if reviewer is None else {"id": reviewer.pk, "username": reviewer.username}


class LeaveRequestCreateSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=leave.REASON_MAX)


class LeaveDecisionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "deny"])
    reason = serializers.CharField(required=False, allow_blank=True, max_length=leave.REASON_MAX)


class MyLeaveRequestEnvelope(serializers.Serializer):
    request = LeaveRequestSerializer(allow_null=True)


def _queryset():
    return OrganizationLeaveRequest.objects.select_related("organization", "user", "reviewed_by", "membership")


class MyLeaveRequestView(OrganizationScopedMixin, APIView):
    """The caller's own request to leave this organization: read the pending (or latest) one, or ask to leave."""

    @extend_schema(responses={200: MyLeaveRequestEnvelope, **ERROR_RESPONSES})
    def get(self, request, organization_id):
        current = leave.current_request(request.user, self.organization)
        return Response({"request": None if current is None else LeaveRequestSerializer(_queryset().get(pk=current.pk)).data})

    @extend_schema(request=LeaveRequestCreateSerializer, responses={201: LeaveRequestSerializer, **ERROR_RESPONSES})
    def post(self, request, organization_id):
        body = LeaveRequestCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        created = leave.request_leave(user=request.user, organization=self.organization, reason=body.validated_data.get("reason", ""), http_request=request)
        return Response(LeaveRequestSerializer(_queryset().get(pk=created.pk)).data, status=status.HTTP_201_CREATED)


class MyLeaveRequestCancelView(OrganizationScopedMixin, APIView):
    @extend_schema(request=None, responses={200: LeaveRequestSerializer, **ERROR_RESPONSES})
    def post(self, request, organization_id):
        cancelled = leave.cancel_leave(user=request.user, organization=self.organization, http_request=request)
        return Response(LeaveRequestSerializer(_queryset().get(pk=cancelled.pk)).data)


class ModeratorLeaveRequestListView(OrganizationScopedMixin, generics.ListAPIView):
    """The organization's leave requests, newest first (owner or moderator; a moderator who sees only their own members sees only those members' requests)."""

    require_owner_or_moderator = True
    serializer_class = LeaveRequestSerializer

    @extend_schema(parameters=[OpenApiParameter("status", str, description="pending, approved, denied, cancelled (comma separated) or all. Default: pending.")])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = self.data_scope.exclude(_queryset().filter(organization=self.organization), owner_field="user_id")
        wanted = self.request.query_params.get("status", "pending")
        if wanted != "all":
            queryset = queryset.filter(status__in=[value for value in wanted.split(",") if value in OrganizationLeaveRequest.Status.values])
        return queryset


class ModeratorLeaveRequestDecisionView(OrganizationScopedMixin, APIView):
    """Approve (suspends that membership) or deny. Moderators decide; an owner or member gets ``403 moderator_required`` from the service."""

    @extend_schema(request=LeaveDecisionSerializer, responses={200: LeaveRequestSerializer, **ERROR_RESPONSES})
    def post(self, request, organization_id, leave_id):
        body = LeaveDecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        target = get_object_or_404(OrganizationLeaveRequest, pk=leave_id, organization=self.organization)
        decided = leave.decide_leave(
            leave=target, actor=request.user, actor_membership=self.active_membership, action=body.validated_data["action"],
            reason=body.validated_data.get("reason", ""), http_request=request,
        )
        return Response(LeaveRequestSerializer(_queryset().get(pk=decided.pk)).data)


class AdminLeaveRequestDecisionView(APIView):
    """FaceCloud-administrator emergency override (an organization with no moderator who can decide): needs a reason of at least 8 characters and is audited."""

    permission_classes = [IsAdminRole]

    @extend_schema(request=LeaveDecisionSerializer, responses={200: LeaveRequestSerializer, **ERROR_RESPONSES})
    def post(self, request, organization_id, leave_id):
        body = LeaveDecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        organization = get_object_or_404(Organization, pk=organization_id)
        target = get_object_or_404(OrganizationLeaveRequest, pk=leave_id, organization=organization)
        decided = leave.decide_leave(
            leave=target, actor=request.user, actor_membership=None, action=body.validated_data["action"], reason=body.validated_data.get("reason", ""),
            admin_override=True, http_request=request,
        )
        return Response(LeaveRequestSerializer(_queryset().get(pk=decided.pk)).data)

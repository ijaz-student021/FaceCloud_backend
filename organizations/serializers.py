from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership

User = get_user_model()


class MemberUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "status", "plan", "is_personal", "created_at", "updated_at"]
        read_only_fields = fields


class OrganizationCreateSerializer(serializers.ModelSerializer):
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="owner", write_only=True,
    )

    class Meta:
        model = Organization
        fields = ["name", "plan", "owner_id"]


class OrganizationMembershipSerializer(serializers.ModelSerializer):
    user = MemberUserSerializer(read_only=True)
    invited_by = MemberUserSerializer(read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = ["id", "organization", "user", "role", "status", "invited_by", "created_at", "updated_at"]
        read_only_fields = fields


class OrganizationMembershipUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=OrganizationMembership.Role.choices, required=False)
    status = serializers.ChoiceField(choices=OrganizationMembership.Status.choices, required=False)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, attrs):
        if "role" not in attrs and "status" not in attrs:
            raise serializers.ValidationError("Provide a role or a status to update.")
        return attrs


class MemberRemovalSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class OrganizationInvitationSerializer(serializers.ModelSerializer):
    invited_by = MemberUserSerializer(read_only=True)

    class Meta:
        model = OrganizationInvitation
        fields = [
            "id", "organization", "email", "role", "status",
            "invited_by", "expires_at", "accepted_at", "created_at",
        ]
        read_only_fields = fields


class OrganizationInvitationCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=OrganizationInvitation.Role.choices, default=OrganizationInvitation.Role.MEMBER,
    )


class OrganizationInvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField()


class OrganizationAuditEventSerializer(serializers.ModelSerializer):
    actor = MemberUserSerializer(read_only=True)

    class Meta:
        model = OrganizationAuditEvent
        fields = [
            "id", "actor", "action", "target_type", "target_id",
            "previous_value", "new_value", "reason", "created_at",
        ]
        read_only_fields = fields

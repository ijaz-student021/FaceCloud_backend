from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .capabilities import membership_capabilities, membership_summary
from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership

User = get_user_model()


class MemberUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "status", "plan", "is_personal", "moderator_data_scope", "created_at", "updated_at"]
        read_only_fields = fields


class OrganizationOwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()


class AdminOrganizationSerializer(OrganizationSerializer):
    """What the administrator sees of every organization: the organization plus who owns it and how many people are in it.

    ``member_count`` counts every active membership (the owner and the moderators included); ``moderator_count`` the active moderators.
    ``owner`` is the organization's oldest active owner, or null when it has none. The view annotates the queryset so a page of
    organizations costs one query, not one per row.
    """

    member_count = serializers.IntegerField(read_only=True)
    moderator_count = serializers.IntegerField(read_only=True)
    owner = serializers.SerializerMethodField()

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ["owner", "member_count", "moderator_count"]
        read_only_fields = fields

    @extend_schema_field(OrganizationOwnerSerializer(allow_null=True))
    def get_owner(self, organization):
        owner_id = getattr(organization, "owner_user_id", None)
        if owner_id is None:
            return None
        return {"id": owner_id, "username": organization.owner_username}


class AdminOrganizationUpdateSerializer(serializers.Serializer):
    """PATCH body for the administrator: any of name, plan, status; ``reason`` is kept in the audit trail only."""

    name = serializers.CharField(max_length=150, required=False)
    plan = serializers.ChoiceField(choices=Organization.Plan.choices, required=False)
    status = serializers.ChoiceField(choices=Organization.Status.choices, required=False)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("The name cannot be empty.")
        return value

    def validate(self, attrs):
        if not any(key in attrs for key in ("name", "plan", "status")):
            raise serializers.ValidationError({"detail": "Send at least one of name, plan or status."})
        return attrs


class MembershipSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    role = serializers.ChoiceField(choices=OrganizationMembership.Role.choices)
    status = serializers.ChoiceField(choices=OrganizationMembership.Status.choices)


class CapabilitiesSerializer(serializers.Serializer):
    """Advisory: what the caller may do here. Every endpoint still enforces it."""

    view_members = serializers.BooleanField()
    invite_members = serializers.BooleanField()
    invite_moderators = serializers.BooleanField()
    manage_members = serializers.BooleanField()
    manage_moderators = serializers.BooleanField()
    grant_owner = serializers.BooleanField()
    view_audit = serializers.BooleanField()
    moderate_reviews = serializers.BooleanField()
    manage_batches = serializers.BooleanField()
    create_reports = serializers.BooleanField()
    export_organization = serializers.BooleanField()


class OrganizationWithMembershipSerializer(OrganizationSerializer):
    """An organization plus the caller's membership and capabilities in it.

    Context: ``memberships`` maps organization id to the caller's membership.
    """

    membership = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ["membership", "capabilities"]
        read_only_fields = fields

    def _membership(self, organization):
        return self.context.get("memberships", {}).get(organization.pk)

    @extend_schema_field(MembershipSummarySerializer(allow_null=True))
    def get_membership(self, organization):
        return membership_summary(self._membership(organization))

    @extend_schema_field(CapabilitiesSerializer)
    def get_capabilities(self, organization):
        return membership_capabilities(self._membership(organization))


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


class OrganizationInvitationPreviewSerializer(serializers.Serializer):
    """What the person who received an invitation sees before accepting it."""

    organization_name = serializers.CharField(source="organization.name")
    email = serializers.EmailField(help_text="The address the invitation was sent to; accepting needs an account with this email.")
    role = serializers.ChoiceField(choices=OrganizationInvitation.Role.choices)
    expires_at = serializers.DateTimeField()


class OrganizationAuditEventSerializer(serializers.ModelSerializer):
    actor = MemberUserSerializer(read_only=True)
    actor_name = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField(help_text="A sentence about what happened, without the actor (show actor_name beside it).")
    target_label = serializers.SerializerMethodField(help_text="The photo, upload or person folder the event was about; null when there is none or it is gone.")
    from_status = serializers.SerializerMethodField(help_text="The review's status before a decision; null for events that record none.")
    to_status = serializers.SerializerMethodField(help_text="The status after a decision; null for events that record none.")

    class Meta:
        model = OrganizationAuditEvent
        fields = [
            "id", "actor", "actor_name", "action", "message", "target_type", "target_id", "target_label",
            "from_status", "to_status", "previous_value", "new_value", "reason", "created_at",
        ]
        read_only_fields = fields

    def _described(self, event):
        from .audit_reading import describe_event, labels_for

        cache = self.context.setdefault("_described", {})
        if event.pk not in cache:
            labels = self.context.get("labels")
            cache[event.pk] = describe_event(event, labels if labels is not None else labels_for([event]))
        return cache[event.pk]

    def get_actor_name(self, event) -> str | None:
        return event.actor.username if event.actor else None

    def get_message(self, event) -> str:
        return self._described(event)[0]

    def get_target_label(self, event) -> str | None:
        return self._described(event)[1]

    def get_from_status(self, event) -> str | None:
        return self._described(event)[2]

    def get_to_status(self, event) -> str | None:
        return self._described(event)[3]

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .capabilities import membership_capabilities, membership_summary
from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
from .services import effective_invitation_status, owner_invitation_of, plan_limits, seats_in_use

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


OWNER_INVITATION_STATUSES = ["pending", "accepted", "cancelled", "expired", "rejected"]


class OwnerInvitationSerializer(serializers.Serializer):
    """The administrator's view of the invitation that makes somebody the owner of an organization that waits for one. Never contains the link or the token."""

    id = serializers.IntegerField()
    email = serializers.EmailField(help_text="The address the invitation was sent to. Only an account holding this verified address can accept it.")
    status = serializers.SerializerMethodField(help_text="pending, accepted, cancelled, expired (a pending invitation whose time has run out) or rejected.")
    expires_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField()
    accepted_at = serializers.DateTimeField(allow_null=True)
    email_status = serializers.CharField(allow_blank=True, help_text=(
        "queued (will be sent once the invitation is saved), sent (the mail server accepted it) or failed (see email_failure_reason). Delivery to the inbox is not known: "
        "SMTP gives no such feedback. Empty only for invitations from before this field existed."))
    email_sent_at = serializers.DateTimeField(allow_null=True)
    email_failure_reason = serializers.CharField(allow_blank=True)
    can_resend = serializers.SerializerMethodField(help_text="True while the organization still waits for its owner: a resend sends a new link and the old one stops working.")
    accepted_by = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=OWNER_INVITATION_STATUSES))
    def get_status(self, invitation):
        return effective_invitation_status(invitation)

    def get_can_resend(self, invitation) -> bool:
        return invitation.organization.status == Organization.Status.PENDING_OWNER and invitation.status != OrganizationInvitation.Status.ACCEPTED

    @extend_schema_field(OrganizationOwnerSerializer(allow_null=True))
    def get_accepted_by(self, invitation):
        if invitation.status != OrganizationInvitation.Status.ACCEPTED:
            return None
        owner = (OrganizationMembership.objects.select_related("user")
                 .filter(organization=invitation.organization, role=OrganizationMembership.Role.OWNER, status=OrganizationMembership.Status.ACTIVE).order_by("created_at", "pk").first())
        return {"id": owner.user_id, "username": owner.user.username} if owner else None


class PlanLimitsSerializer(serializers.Serializer):
    moderators = serializers.IntegerField(allow_null=True, help_text="null means unlimited.")
    members = serializers.IntegerField(allow_null=True, help_text="null means unlimited.")


class PlanUsageSerializer(serializers.Serializer):
    moderators = serializers.IntegerField()
    members = serializers.IntegerField()


class PlanFiguresMixin(serializers.Serializer):
    """``limits`` (what the plan allows; null = unlimited) and ``usage`` (the seats taken: active people plus invitations still waiting, which hold a seat).

    Read from the ``moderator_seats`` / ``member_seats`` annotations of ``organizations_with_head_counts()`` (one query for a page), or counted when absent.
    """

    limits = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    @extend_schema_field(PlanLimitsSerializer)
    def get_limits(self, organization):
        return plan_limits(organization)

    @extend_schema_field(PlanUsageSerializer)
    def get_usage(self, organization):
        seats = {"moderators": getattr(organization, "moderator_seats", None), "members": getattr(organization, "member_seats", None)}
        return {kind: count if count is not None else seats_in_use(organization, kind) for kind, count in seats.items()}


class AdminOrganizationSerializer(PlanFiguresMixin, OrganizationSerializer):
    """What the administrator sees of every organization: the organization plus who owns it and how many people are in it.

    ``member_count`` counts every active membership (the owner and the moderators included); ``moderator_count`` the active moderators.
    ``owner`` is the organization's oldest active owner, or null when it has none. The view annotates the queryset so a page of
    organizations costs one query, not one per row.
    """

    member_count = serializers.IntegerField(read_only=True)
    moderator_count = serializers.IntegerField(read_only=True)
    owner = serializers.SerializerMethodField()
    owner_invitation = serializers.SerializerMethodField(help_text="While the organization waits for its owner (`status: pending_owner`): the invitation sent to them; otherwise null.")

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ["owner", "owner_invitation", "member_count", "moderator_count", "limits", "usage"]
        read_only_fields = fields

    @extend_schema_field(OwnerInvitationSerializer(allow_null=True))
    def get_owner_invitation(self, organization):
        if organization.status != Organization.Status.PENDING_OWNER:
            return None
        invitation = owner_invitation_of(organization)
        return OwnerInvitationSerializer(invitation).data if invitation is not None else None

    @extend_schema_field(OrganizationOwnerSerializer(allow_null=True))
    def get_owner(self, organization):
        owner_id = getattr(organization, "owner_user_id", None)
        if owner_id is None:
            return None
        return {"id": owner_id, "username": organization.owner_username}


class AdminOrganizationUpdateSerializer(serializers.Serializer):
    """PATCH body for the administrator: any of name, plan, status, moderator_data_scope; ``reason`` is kept in the audit trail only."""

    name = serializers.CharField(max_length=150, required=False)
    plan = serializers.ChoiceField(choices=Organization.Plan.choices, required=False)
    status = serializers.ChoiceField(
        choices=[(Organization.Status.ACTIVE, "Active"), (Organization.Status.SUSPENDED, "Suspended")], required=False,
        help_text="active or suspended. An organization waiting for its owner (`pending_owner`) becomes active only when the owner accepts the invitation.",
    )
    moderator_data_scope = serializers.ChoiceField(choices=Organization.ModeratorDataScope.choices, required=False)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)
    CHANGES = ("name", "plan", "status", "moderator_data_scope")

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("The name cannot be empty.")
        return value

    def validate(self, attrs):
        if not any(key in attrs for key in self.CHANGES):
            raise serializers.ValidationError({"detail": "Send at least one of name, plan, status or moderator_data_scope."})
        return attrs


class OwnerOrganizationUpdateSerializer(serializers.Serializer):
    """PATCH body for the organization's owner: ``name``, ``moderator_data_scope`` and the branding (``brand_name``, ``brand_accent``).

    The plan and the status are the administrator's (the view answers 403 if they are sent); the branding is the owner's alone. A blank brand name or
    accent clears it. The logo is uploaded separately (``PUT /api/organizations/{id}/logo/``).
    """

    name = serializers.CharField(max_length=150, required=False)
    moderator_data_scope = serializers.ChoiceField(choices=Organization.ModeratorDataScope.choices, required=False)
    brand_name = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, help_text="Shown to members instead of the name; blank clears it. At most 100 characters.")
    brand_accent = serializers.CharField(required=False, allow_blank=True, help_text="A colour like #1a73e8 (or #1ae); stored as #rrggbb. Blank clears it.")
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)
    CHANGES = ("name", "moderator_data_scope", "brand_name", "brand_accent")

    validate_name = AdminOrganizationUpdateSerializer.validate_name

    def validate_brand_name(self, value):
        from .branding import clean_brand_name

        return clean_brand_name(value)

    def validate_brand_accent(self, value):
        from .branding import clean_accent

        return clean_accent(value)

    def validate(self, attrs):
        if not any(key in attrs for key in self.CHANGES):
            raise serializers.ValidationError({"detail": "Send at least one of name, moderator_data_scope, brand_name or brand_accent."})
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
    edit_organization = serializers.BooleanField()


class OrganizationWithMembershipSerializer(OrganizationSerializer):
    """An organization plus the caller's membership and capabilities in it.

    Context: ``memberships`` maps organization id to the caller's membership.
    """

    membership = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    logo_url = serializers.SerializerMethodField(
        help_text="Authenticated URL of the organization's logo (it changes when the logo is replaced), or null. Fetch it with the Authorization header.",
    )

    class Meta(OrganizationSerializer.Meta):
        fields = OrganizationSerializer.Meta.fields + ["brand_name", "brand_accent", "logo_url", "membership", "capabilities"]
        read_only_fields = fields

    def get_logo_url(self, organization) -> str | None:
        if not organization.logo:
            return None
        version = int(organization.logo_updated_at.timestamp() * 1000) if organization.logo_updated_at else 0           # milliseconds: two replacements in one second differ
        return f"/api/organizations/{organization.pk}/logo/?v={version}"

    def _membership(self, organization):
        return self.context.get("memberships", {}).get(organization.pk)

    @extend_schema_field(MembershipSummarySerializer(allow_null=True))
    def get_membership(self, organization):
        return membership_summary(self._membership(organization))

    @extend_schema_field(CapabilitiesSerializer)
    def get_capabilities(self, organization):
        return membership_capabilities(self._membership(organization))


class OrganizationDetailSerializer(PlanFiguresMixin, OrganizationWithMembershipSerializer):
    """One organization as its own people see it. The owner and the moderators also get who owns it and how many people are in it; a plain member does not.

    ``member_count`` counts every active membership (the owner and the moderators included), ``moderator_count`` the active moderators, ``owner`` is the
    oldest active owner or null. The instance comes from ``organizations_with_head_counts()`` (one query).
    """

    owner = serializers.SerializerMethodField()
    member_count = serializers.IntegerField(read_only=True, required=False)
    moderator_count = serializers.IntegerField(read_only=True, required=False)
    STAFF_ONLY = ("owner", "member_count", "moderator_count", "limits", "usage")

    class Meta(OrganizationWithMembershipSerializer.Meta):
        fields = OrganizationWithMembershipSerializer.Meta.fields + ["owner", "member_count", "moderator_count", "limits", "usage"]
        read_only_fields = fields

    @extend_schema_field(OrganizationOwnerSerializer(allow_null=True))
    def get_owner(self, organization):
        owner_id = getattr(organization, "owner_user_id", None)
        return None if owner_id is None else {"id": owner_id, "username": organization.owner_username}

    def to_representation(self, organization):
        data = super().to_representation(organization)
        membership = self._membership(organization)
        if membership is None or membership.role not in (OrganizationMembership.Role.OWNER, OrganizationMembership.Role.MODERATOR):
            for key in self.STAFF_ONLY:
                data.pop(key, None)
        return data


class OrganizationCreateSerializer(serializers.Serializer):
    """The administrator creates an organization with ``owner_email`` (the owner needs no account yet and accepts an invitation) or, as before, with ``owner_id`` (an existing account becomes the owner at once). Send exactly one."""

    name = serializers.CharField(max_length=150)
    plan = serializers.ChoiceField(choices=Organization.Plan.choices, required=False, default=Organization.Plan.FREE)
    owner_email = serializers.EmailField(required=False, help_text="The future owner's address. They receive an invitation; nothing is granted until they accept it as this address. They may have an account or not.")
    owner_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), source="owner", write_only=True, required=False, help_text="Existing behaviour: this account becomes the owner immediately, without an invitation.")

    def validate_name(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("The name cannot be empty.")
        return value

    def validate(self, attrs):
        has_email, has_owner = bool(attrs.get("owner_email")), attrs.get("owner") is not None
        if has_email == has_owner:
            raise serializers.ValidationError({"owner_email": "Send the owner's email address (owner_email). An existing account's id (owner_id) is accepted instead, but not both."})
        return attrs


class AdminOrganizationCreatedSerializer(AdminOrganizationSerializer):
    """The organization as the administrator list shows it, plus whether this request created it."""

    created = serializers.BooleanField(help_text="False when the same request (same name and owner e-mail, still waiting for the owner) had already been made: the existing organization is returned and no e-mail is sent.")
    owner_account_exists = serializers.BooleanField(allow_null=True, help_text="For an owner_email request: whether an account with that address exists (it will be asked to sign in) or not (it will register). null for an owner_id request.")

    class Meta(AdminOrganizationSerializer.Meta):
        fields = AdminOrganizationSerializer.Meta.fields + ["created", "owner_account_exists"]
        read_only_fields = fields


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
        choices=[(OrganizationInvitation.Role.MODERATOR, "Moderator"), (OrganizationInvitation.Role.MEMBER, "Member")], default=OrganizationInvitation.Role.MEMBER,
    )


class OrganizationInvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=256)


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

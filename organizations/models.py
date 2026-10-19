from django.conf import settings
from django.db import models
from django.utils import timezone


class Organization(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    class Plan(models.TextChoices):
        FREE = "free", "Free"
        TEAM = "team", "Team"
        ENTERPRISE = "enterprise", "Enterprise"

    class ModeratorDataScope(models.TextChoices):
        OWN_MEMBERS = "own_members", "Each moderator sees their own members"
        ORGANIZATION = "organization", "Every moderator sees the whole organization"

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=60, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    # What a MODERATOR of this organization can see (an owner always sees everything). New organizations start with ``own_members``; organizations
    # that existed before this setting keep ``organization`` (their behaviour is unchanged until the owner or the administrator chooses).
    # Enforced on the server by organizations/scope.py.
    moderator_data_scope = models.CharField(max_length=16, choices=ModeratorDataScope.choices, default=ModeratorDataScope.OWN_MEMBERS)
    plan = models.CharField(max_length=16, choices=Plan.choices, default=Plan.FREE)
    # Auto-created alongside every account (see organizations.services.create_personal_organization)
    # so existing user-scoped ownership keeps working unchanged until someone is invited in.
    is_personal = models.BooleanField(default=False)
    storage_quota_bytes = models.BigIntegerField(null=True, blank=True)
    upload_quota_per_day = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class OrganizationMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MODERATOR = "moderator", "Moderator"
        MEMBER = "member", "Member"

    class Status(models.TextChoices):
        INVITED = "invited", "Invited"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        REMOVED = "removed", "Removed"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="organizations_unique_membership",
            ),
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self):
        return f"user {self.user_id} in org {self.organization_id} ({self.role})"


class OrganizationInvitation(models.Model):
    class Role(models.TextChoices):
        MODERATOR = "moderator", "Moderator"
        MEMBER = "member", "Member"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invitations",
    )
    email = models.EmailField(max_length=254)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"], condition=models.Q(status="pending"),
                name="organizations_unique_pending_invite",
            ),
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def is_expired(self, now=None):
        return self.expires_at <= (now or timezone.now())

    def __str__(self):
        return f"invite {self.email} to org {self.organization_id}"


class OrganizationAuditEvent(models.Model):
    """Immutable audit trail: no update or delete is ever exposed via the API."""

    class Action(models.TextChoices):
        ORGANIZATION_CREATED = "organization_created", "Organization created"
        MEMBER_INVITED = "member_invited", "Member invited"
        INVITATION_RESENT = "invitation_resent", "Invitation resent"
        INVITATION_CANCELLED = "invitation_cancelled", "Invitation cancelled"
        INVITATION_ACCEPTED = "invitation_accepted", "Invitation accepted"
        MEMBER_ROLE_CHANGED = "member_role_changed", "Member role changed"
        MEMBER_STATUS_CHANGED = "member_status_changed", "Member status changed"
        MEMBER_REMOVED = "member_removed", "Member removed"
        MODERATOR_REVIEW_DECIDED = "moderator_review_decided", "Moderator review decided"
        MODERATOR_REVIEW_ESCALATED = "moderator_review_escalated", "Moderator review escalated"
        ORGANIZATION_DATA_EXPORTED = "organization_data_exported", "Organization data exported"
        MODERATOR_REVIEW_ASSIGNED = "moderator_review_assigned", "Moderator review assigned"
        BATCH_RETRIED_BY_MODERATOR = "batch_retried_by_moderator", "Batch retried by moderator"
        BATCH_CANCELLED_BY_MODERATOR = "batch_cancelled_by_moderator", "Batch cancelled by moderator"
        IDENTITY_GROUPS_MERGED_BY_MODERATOR = "identity_groups_merged_by_moderator", "Identity groups merged by moderator"
        MODERATOR_REVIEW_CLAIMED = "moderator_review_claimed", "Moderator review claimed"
        MODERATOR_REVIEW_RELEASED = "moderator_review_released", "Moderator review released"
        MODERATOR_BULK_DECISION = "moderator_bulk_decision", "Moderator bulk decision"
        MODERATOR_IMAGE_PREVIEWED = "moderator_image_previewed", "Image previewed by moderator"
        BATCH_REPROCESSED_BY_MODERATOR = "batch_reprocessed_by_moderator", "Batch reprocessed by moderator"
        MEMBER_NOTIFIED_OF_REVIEW = "member_notified_of_review", "Member told about a review decision"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    action = models.CharField(max_length=40, choices=Action.choices)
    target_type = models.CharField(max_length=32, blank=True)
    target_id = models.PositiveIntegerField(null=True, blank=True)
    previous_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self):
        return f"{self.action} in org {self.organization_id}"

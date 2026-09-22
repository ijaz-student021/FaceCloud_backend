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

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=60, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
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

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
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

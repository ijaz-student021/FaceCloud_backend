import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


def avatar_upload_path(instance, filename):
    return f"users/{instance.pk}/avatar/{uuid.uuid4().hex}.png"


class User(AbstractUser):
    ROLE_CHOICES = [("user", "User"), ("moderator", "Moderator"), ("admin", "Admin")]
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="user")
    is_suspended = models.BooleanField(default=False)
    suspension_reason = models.TextField(blank=True)
    is_email_verified = models.BooleanField(default=False)
    token_version = models.PositiveIntegerField(default=0)
    # Profile details the user may fill in; all optional. organization_name is
    # free text describing the user, never an access-control input: access to an
    # organization comes only from OrganizationMembership.
    display_name = models.CharField(max_length=150, blank=True)
    organization_name = models.CharField(max_length=150, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=80, blank=True)
    # Re-encoded on upload (accounts/avatars.py); served only to the user.
    avatar = models.FileField(upload_to=avatar_upload_path, blank=True)
    avatar_updated_at = models.DateTimeField(null=True, blank=True)


class AuthEvent(models.Model):
    EVENT_CHOICES = [
        ("registration_pending", "Registration pending"),
        ("verification_code_sent", "Verification code sent"),
        ("registered", "Registered"),
        ("email_verified", "Email verified"),
        ("login", "Login"),
        ("login_failed", "Login failed"),
        ("login_locked", "Login temporarily locked"),
        ("logout", "Logout"),
        ("logout_all", "Logout from all devices"),
        ("session_revoked", "Session signed out"),
        ("account_invited", "Account invitation sent"),
        ("invitation_accepted", "Account invitation accepted"),
        ("password_changed", "Password changed"),
        ("password_reset", "Password reset"),
        ("profile_updated", "Profile updated"),
        ("role_changed", "Role changed"),
        ("suspended", "Suspended"),
        ("reactivated", "Reactivated"),
        ("account_deleted", "Account deleted"),
        # Two-factor authentication was removed; these three stay so audit rows written earlier remain readable.
        ("two_factor_enabled", "Two-factor authentication enabled"),
        ("two_factor_disabled", "Two-factor authentication disabled"),
        ("two_factor_recovery_codes_regenerated", "Two-factor recovery codes regenerated"),
    ]
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="auth_events",
    )
    event = models.CharField(max_length=40, choices=EVENT_CHOICES)
    identifier = models.CharField(max_length=254, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class LoginAttempt(models.Model):
    """Failed-login counter used for per-account backoff (see accounts/lockout.py).

    Keyed by a keyed hash of the account (or, for an identifier that matches no
    account, of the identifier), so the table neither stores emails nor treats
    unknown identifiers differently from real ones.
    """

    key_hash = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)


class PendingRegistration(models.Model):
    STATUS_PENDING = "pending"
    STATUS_CONSUMED = "consumed"
    STATUS_EXPIRED = "expired"
    STATUS_LOCKED = "locked"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CONSUMED, "Consumed"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_LOCKED, "Locked"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    ACTIVE_STATUSES = [STATUS_PENDING, STATUS_LOCKED]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    username = models.CharField(max_length=150)
    email = models.EmailField(max_length=254)
    password_hash = models.CharField(max_length=128)
    code_hash = models.CharField(max_length=128)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    resend_count = models.PositiveSmallIntegerField(default=1)
    resend_window_started_at = models.DateTimeField()
    code_expires_at = models.DateTimeField()
    resend_available_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_user = models.OneToOneField(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="signup_registration",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                Lower("username"),
                condition=models.Q(status__in=["pending", "locked"]),
                name="accounts_unique_active_pending_username_ci",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=models.Q(status__in=["pending", "locked"]),
                name="accounts_unique_active_pending_email_ci",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "code_expires_at"],
                name="acct_pending_status_exp_idx",
            ),
            models.Index(fields=["email"], name="accounts_pending_email_idx"),
        ]


class UserSession(models.Model):
    """One signed-in device: created at login, carried in tokens as ``sid``.

    Refresh rotation keeps ``refresh_jti`` and ``expires_at`` current. A session
    has ended when it is revoked, when its refresh token has expired, or when
    the user's ``token_version`` has moved on (logout-all, password change or
    reset, suspension): the same rule the authentication class applies.
    Records of ended sessions are deleted after SESSION_RECORD_RETENTION_DAYS.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sessions")
    token_version = models.PositiveIntegerField()
    refresh_jti = models.CharField(max_length=255, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["-last_seen_at"]
        indexes = [models.Index(fields=["user", "-last_seen_at"])]

    def is_active_for(self, user, now):
        return self.revoked_at is None and self.expires_at > now and self.token_version == user.token_version


class AccountInvitation(models.Model):
    """An administrator's invitation to create an account with a given role.

    Accepting proves control of the mailbox (the token arrives only there), so
    the account is created with its email verified. Only the SHA-256 of the
    token is stored.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    email = models.EmailField()
    role = models.CharField(max_length=20, choices=User.ROLE_CHOICES)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    accepted_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(Lower("email"), condition=models.Q(status="pending"),
                                    name="accounts_one_pending_invitation_per_email"),
        ]

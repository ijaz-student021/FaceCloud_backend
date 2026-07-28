import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class User(AbstractUser):
    ROLE_CHOICES = [("user", "User"), ("moderator", "Moderator"), ("admin", "Admin")]
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="user")
    is_suspended = models.BooleanField(default=False)
    suspension_reason = models.TextField(blank=True)
    is_email_verified = models.BooleanField(default=False)
    token_version = models.PositiveIntegerField(default=0)


class AuthEvent(models.Model):
    EVENT_CHOICES = [
        ("registration_pending", "Registration pending"),
        ("verification_code_sent", "Verification code sent"),
        ("registered", "Registered"),
        ("email_verified", "Email verified"),
        ("login", "Login"),
        ("login_failed", "Login failed"),
        ("logout", "Logout"),
        ("logout_all", "Logout from all devices"),
        ("password_changed", "Password changed"),
        ("password_reset", "Password reset"),
        ("profile_updated", "Profile updated"),
        ("role_changed", "Role changed"),
        ("suspended", "Suspended"),
        ("reactivated", "Reactivated"),
        ("account_deleted", "Account deleted"),
    ]
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="auth_events",
    )
    event = models.CharField(max_length=32, choices=EVENT_CHOICES)
    identifier = models.CharField(max_length=254, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


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

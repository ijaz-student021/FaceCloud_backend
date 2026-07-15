from django.contrib.auth.models import AbstractUser
from django.db import models


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

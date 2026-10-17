from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuthEvent, PendingRegistration, User


@admin.register(User)
class FaceCloudUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("FaceCloud", {"fields": ("role", "is_email_verified", "is_suspended", "suspension_reason", "token_version")}),
    )
    list_display = UserAdmin.list_display + ("role", "is_email_verified", "is_suspended")


@admin.register(AuthEvent)
class AuthEventAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "identifier", "ip_address", "created_at")
    list_filter = ("event", "created_at")
    search_fields = ("user__username", "user__email", "identifier")
    readonly_fields = ("user", "event", "identifier", "ip_address", "details", "created_at")


@admin.register(PendingRegistration)
class PendingRegistrationAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "username",
        "full_name",
        "email",
        "status",
        "failed_attempts",
        "token_expires_at",
        "created_at",
    )
    list_filter = ("status", "created_at", "token_expires_at")
    search_fields = ("public_id", "username", "email")
    readonly_fields = (
        "public_id",
        "username",
        "full_name",
        "email",
        "password_hash",
        "token_hash",
        "status",
        "failed_attempts",
        "resend_count",
        "resend_window_started_at",
        "token_expires_at",
        "resend_available_at",
        "consumed_at",
        "created_user",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

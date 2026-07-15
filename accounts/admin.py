from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuthEvent, User


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

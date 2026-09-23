from django.contrib import admin

from .models import Organization, OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "plan", "is_personal", "created_at")
    search_fields = ("name", "slug")
    list_filter = ("status", "plan", "is_personal")


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "role", "status", "created_at")
    list_filter = ("role", "status")
    search_fields = ("organization__name", "user__username", "user__email")


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    exclude = ("token_hash",)
    list_display = ("organization", "email", "role", "status", "expires_at")
    list_filter = ("role", "status")
    search_fields = ("organization__name", "email")


@admin.register(OrganizationAuditEvent)
class OrganizationAuditEventAdmin(admin.ModelAdmin):
    list_display = ("organization", "actor", "action", "target_type", "target_id", "created_at")
    list_filter = ("action",)
    search_fields = ("organization__name",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

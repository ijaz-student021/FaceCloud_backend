from django.contrib import admin

from .models import BiometricConsent, PrivacyRequest


@admin.register(BiometricConsent)
class BiometricConsentAdmin(admin.ModelAdmin):
    list_display = ("user", "purpose", "granted", "policy_version", "created_at")
    list_filter = ("purpose", "granted", "policy_version")
    readonly_fields = [field.name for field in BiometricConsent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PrivacyRequest)
class PrivacyRequestAdmin(admin.ModelAdmin):
    list_display = ("kind", "trigger", "status", "verified", "user", "created_at")
    list_filter = ("kind", "trigger", "status", "verified")
    readonly_fields = [field.name for field in PrivacyRequest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

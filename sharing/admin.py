from django.contrib import admin

from .models import ShareLink


@admin.register(ShareLink)
class ShareLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "enhancement_result", "expires_at", "revoked_at", "access_count")
    list_filter = ("revoked_at",)
    # The token hash is left out on purpose; nothing in the admin should help
    # reconstruct or brute-force a link.
    exclude = ("token_hash",)
    readonly_fields = [
        field.name for field in ShareLink._meta.fields if field.name != "token_hash"
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

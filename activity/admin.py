from django.contrib import admin

from .models import ActivityEvent


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ("category", "event", "user", "object_type", "object_id", "created_at")
    list_filter = ("category", "event", "created_at")
    search_fields = ("user__username", "user__email", "object_type", "object_id")
    readonly_fields = ("user", "category", "event", "object_type", "object_id", "details", "created_at")

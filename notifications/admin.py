from django.contrib import admin

from .models import NotificationPreference, UserNotification


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "email_notifications", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(UserNotification)
class UserNotificationAdmin(admin.ModelAdmin):
    list_display = (
        "recipient", "event_type", "severity", "email_status", "read_at", "created_at",
    )
    list_filter = ("event_type", "severity", "email_status")
    search_fields = ("recipient__username", "recipient__email", "title")
    readonly_fields = ("created_at", "updated_at", "email_sent_at")

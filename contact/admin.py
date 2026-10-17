from django.contrib import admin

from .models import ContactMessage


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "name", "email", "reason", "delivery_status")
    list_filter = ("reason", "delivery_status")
    readonly_fields = [field.name for field in ContactMessage._meta.fields]

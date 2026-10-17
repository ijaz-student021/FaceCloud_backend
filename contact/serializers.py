from rest_framework import serializers

from .models import ContactMessage


class ContactMessageSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    email = serializers.EmailField(max_length=254)
    company = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    reason = serializers.ChoiceField(choices=ContactMessage.Reason.choices)
    message = serializers.CharField(min_length=10, max_length=5000)
    website = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="Leave empty. A spam trap: forms that fill it are accepted but not delivered.",
    )


class ContactAcceptedSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    status = serializers.ChoiceField(choices=["queued"])
    detail = serializers.CharField()

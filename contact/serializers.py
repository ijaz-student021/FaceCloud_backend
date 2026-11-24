from rest_framework import serializers

from .models import ContactMessage, EarlyAccessSignup


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


class EarlyAccessRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    website = serializers.CharField(required=False, allow_blank=True, default="", help_text="Leave empty. A spam trap.")


class EarlyAccessAcceptedSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["registered", "already_registered"])
    detail = serializers.CharField()


class AdminContactMessageSerializer(serializers.ModelSerializer):
    """What an administrator sees of a contact form message: everything the person filled in, and whether it has been read."""

    is_read = serializers.SerializerMethodField()
    reason = serializers.CharField(read_only=True)          # the words the person chose, as a plain string (a choice list here collides with the other "reason" enums in the schema)
    delivery_status = serializers.CharField(read_only=True)

    class Meta:
        model = ContactMessage
        fields = ("id", "name", "email", "company", "reason", "message", "delivery_status", "delivered_at", "failure_reason", "is_read", "read_at", "created_at")
        read_only_fields = fields

    def get_is_read(self, obj) -> bool:
        return obj.read_at is not None


class AdminEarlyAccessSerializer(serializers.ModelSerializer):
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = EarlyAccessSignup
        fields = ("id", "email", "source", "is_read", "read_at", "created_at")
        read_only_fields = fields

    def get_is_read(self, obj) -> bool:
        return obj.read_at is not None


class MarkReadSerializer(serializers.Serializer):
    read = serializers.BooleanField(default=True)


class MarkAllReadSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["contact", "early_access", "all"], default="all")


class ContactAcceptedSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    status = serializers.ChoiceField(choices=["queued"])
    detail = serializers.CharField()

from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from enhancement.models import EnhancementStatus, ImageEditVersion, ImageEnhancementResult
from enhancement.serializers import WatermarkOptionsSerializer

from .models import ShareLink, ShareLinkAccess


class ShareLinkCreateSerializer(WatermarkOptionsSerializer):
    """Request body: what to share, for how long, and (optionally) how to mark it."""

    result = serializers.IntegerField(help_text="Id of a completed enhancement result you own.")
    edit = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="Optional id of a completed edit of that result to show instead.",
    )
    ttl_hours = serializers.IntegerField(
        required=False, min_value=1,
        help_text="How long the link works. Defaults to SHARE_LINK_DEFAULT_TTL_HOURS (24).",
    )
    label = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate_ttl_hours(self, value):
        if value > settings.SHARE_LINK_MAX_TTL_HOURS:
            raise serializers.ValidationError(
                f"A link can last at most {settings.SHARE_LINK_MAX_TTL_HOURS} hours."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        owner = self.context["request"].user
        result = ImageEnhancementResult.objects.filter(
            owner=owner, status=EnhancementStatus.COMPLETED, pk=attrs["result"],
        ).exclude(enhanced_file="").first()
        if result is None:
            raise serializers.ValidationError({"result": ["No completed result with that id."]})
        attrs["result_object"] = result
        attrs["edit_object"] = None
        if attrs.get("edit") is not None:
            edit = ImageEditVersion.objects.filter(
                owner=owner, enhancement_result=result,
                status=EnhancementStatus.COMPLETED, pk=attrs["edit"],
            ).exclude(edited_file="").first()
            if edit is None:
                raise serializers.ValidationError(
                    {"edit": ["No completed edit of that result with that id."]}
                )
            attrs["edit_object"] = edit
        return attrs


class ShareLinkAccessSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShareLinkAccess
        fields = ["accessed_at", "user_agent"]


class ShareLinkSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()
    result = serializers.IntegerField(source="enhancement_result_id", read_only=True)
    edit = serializers.IntegerField(source="edit_version_id", read_only=True)
    identity_export = serializers.IntegerField(source="identity_export_id", read_only=True)

    class Meta:
        model = ShareLink
        fields = [
            "id", "label", "state", "result", "edit", "watermark", "expires_at",
            "revoked_at", "access_count", "last_accessed_at", "created_at",
            "identity_export",
        ]
        read_only_fields = fields

    def get_state(self, obj) -> str:
        return obj.state()


class ShareLinkDetailSerializer(ShareLinkSerializer):
    recent_accesses = serializers.SerializerMethodField()

    class Meta(ShareLinkSerializer.Meta):
        fields = ShareLinkSerializer.Meta.fields + ["recent_accesses"]
        read_only_fields = fields

    @extend_schema_field(ShareLinkAccessSerializer(many=True))
    def get_recent_accesses(self, obj):
        return ShareLinkAccessSerializer(obj.accesses.all()[:20], many=True).data


class ShareLinkCreatedSerializer(ShareLinkSerializer):
    """Returned once, at creation: the only time the raw token is available."""

    token = serializers.CharField(read_only=True)
    public_url = serializers.SerializerMethodField()
    info_url = serializers.SerializerMethodField()

    class Meta(ShareLinkSerializer.Meta):
        fields = ShareLinkSerializer.Meta.fields + ["token", "public_url", "info_url"]
        read_only_fields = fields

    def _absolute(self, name, obj):
        path = reverse(name, kwargs={"token": obj.token})
        return settings.PUBLIC_BACKEND_URL.rstrip("/") + path

    def get_public_url(self, obj) -> str:
        return self._absolute("sharing:public-view", obj)

    def get_info_url(self, obj) -> str:
        return self._absolute("sharing:public-info", obj)

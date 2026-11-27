from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import DownloadGrant, DownloadRequest, DownloadRequestItem

Artifact = DownloadRequestItem.ArtifactType
DENIAL_CODES = [value for value in DownloadRequestItem.DenialReason.values if value != DownloadRequestItem.DenialReason.NO_LONGER_ELIGIBLE]


class RequestedImageSerializer(serializers.Serializer):
    """One image of a request or an eligibility check. ``artifact_type`` defaults to the enhanced result; ``edited`` needs ``artifact_id`` (the saved edit)."""

    image_id = serializers.IntegerField(min_value=1)
    artifact_type = serializers.ChoiceField(choices=Artifact.choices, required=False, default=Artifact.ENHANCED)
    artifact_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class DownloadRequestCreateSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)
    items = RequestedImageSerializer(many=True, allow_empty=False)
    message = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    client_token = serializers.CharField(max_length=64, required=False, allow_blank=True, default="", help_text="Your own token for this submission: sending it again returns the request that already exists.")


class EligibilityRequestSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)
    items = RequestedImageSerializer(many=True, allow_empty=False)


class EligibilityRowSerializer(serializers.Serializer):
    image_id = serializers.IntegerField()
    artifact_type = serializers.CharField()
    eligible = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True, help_text="Why it cannot be requested (a code), empty when it can.")
    detail = serializers.CharField(allow_blank=True)
    access_state = serializers.ChoiceField(choices=["not_requested", "pending", "granted", "denied", "expired", "revoked"])


class EligibilityResponseSerializer(serializers.Serializer):
    approval_required = serializers.BooleanField()
    items = EligibilityRowSerializer(many=True)


class DownloadGrantSerializer(serializers.ModelSerializer):
    active = serializers.SerializerMethodField()
    expired = serializers.SerializerMethodField()

    class Meta:
        model = DownloadGrant
        fields = ["id", "artifact_type", "artifact_id", "expires_at", "revoked_at", "revoke_reason", "max_uses", "use_count", "active", "expired", "created_at"]
        read_only_fields = fields

    def get_active(self, grant) -> bool:
        from django.utils import timezone

        return grant.revoked_at is None and grant.expires_at > timezone.now()

    def get_expired(self, grant) -> bool:
        from django.utils import timezone

        return grant.revoked_at is None and grant.expires_at <= timezone.now()


class DownloadRequestItemSerializer(serializers.ModelSerializer):
    image_name = serializers.CharField(source="image.original_name", read_only=True)
    grant = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = DownloadRequestItem
        fields = [
            "id", "image_id", "image_name", "artifact_type", "artifact_id", "status", "denial_reason_code", "denial_reason_text", "reviewed_at", "grant", "file_url",
        ]
        read_only_fields = fields

    @extend_schema_field(DownloadGrantSerializer(allow_null=True))
    def get_grant(self, item):
        grant = getattr(item, "grant", None) if item.status == DownloadRequestItem.Status.APPROVED else None
        return DownloadGrantSerializer(grant).data if grant is not None else None

    def get_file_url(self, item) -> str | None:
        """Where the requester downloads this file, once approved (the endpoint still checks the grant on every call)."""
        if item.status != DownloadRequestItem.Status.APPROVED or self.context.get("for_requester") is not True:
            return None
        return f"/api/downloads/requests/{item.request_id}/items/{item.pk}/file/"


class DownloadRequestCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    pending = serializers.IntegerField()
    approved = serializers.IntegerField()
    denied = serializers.IntegerField()


class DownloadRequestSerializer(serializers.ModelSerializer):
    """A request without its items (lists)."""

    organization_name = serializers.CharField(source="organization.name", read_only=True)
    requester = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()

    class Meta:
        model = DownloadRequest
        fields = [
            "id", "organization", "organization_name", "requester", "status", "requester_message", "version", "counts", "created_at", "updated_at", "decided_at",
        ]
        read_only_fields = fields

    @extend_schema_field({"type": "object", "properties": {"id": {"type": "integer"}, "username": {"type": "string"}, "email": {"type": "string"}}})
    def get_requester(self, download_request):
        user = download_request.requester
        return {"id": user.pk, "username": user.get_username(), "email": user.email}

    @extend_schema_field(DownloadRequestCountsSerializer)
    def get_counts(self, download_request):
        items = list(download_request.items.all())                       # prefetched by the views
        return {
            "total": len(items),
            "pending": sum(1 for item in items if item.status == DownloadRequestItem.Status.PENDING),
            "approved": sum(1 for item in items if item.status == DownloadRequestItem.Status.APPROVED),
            "denied": sum(1 for item in items if item.status == DownloadRequestItem.Status.DENIED),
        }


class DownloadRequestDetailSerializer(DownloadRequestSerializer):
    items = DownloadRequestItemSerializer(many=True, read_only=True)

    class Meta(DownloadRequestSerializer.Meta):
        fields = DownloadRequestSerializer.Meta.fields + ["items"]
        read_only_fields = fields


class DownloadRequestCreatedSerializer(serializers.Serializer):
    created = serializers.BooleanField(help_text="False when client_token named a request that already existed (a replay).")
    request = DownloadRequestDetailSerializer()


class DenialReasonSerializer(serializers.Serializer):
    code = serializers.ChoiceField(choices=DENIAL_CODES)
    text = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class DownloadDecisionSerializer(serializers.Serializer):
    """``approve_all``: every undecided image. ``approve_selected``: the images in ``item_ids``; the other undecided ones are denied with ``reason``. ``deny_selected``: the
    images in ``item_ids``; the rest stay undecided. ``deny_all``: every undecided image. A denial needs ``reason`` (or ``item_reasons`` for each image); ``other`` needs text."""

    action = serializers.ChoiceField(choices=["approve_all", "approve_selected", "deny_selected", "deny_all"])
    item_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, allow_empty=True)
    reason = DenialReasonSerializer(required=False)
    item_reasons = serializers.DictField(child=DenialReasonSerializer(), required=False, help_text="Reason per item id, which overrides `reason` for that image.")
    expected_version = serializers.IntegerField(min_value=0, required=False, help_text="The version you saw; a different one answers 409 request_changed.")


class GrantRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

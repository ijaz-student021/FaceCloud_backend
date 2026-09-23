from rest_framework import serializers

from .models import (IdentityAnalysis, IdentityChange, IdentityGroup,
                     IdentityGroupExport, IdentityMembership)


class IdentityAnalyzeRequestSerializer(serializers.Serializer):
    force = serializers.BooleanField(default=False, required=False)


class IdentityAnalysisSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityAnalysis
        fields = ("id", "image", "status", "face_count", "assigned_count", "review_count", "provider", "policy_version", "failure_reason", "processing_duration_ms", "started_at", "finished_at", "created_at", "updated_at")


class IdentityGroupSerializer(serializers.ModelSerializer):
    preview_image = serializers.SerializerMethodField()

    class Meta:
        model = IdentityGroup
        fields = ("id", "display_name", "state", "member_count", "preview_image", "created_at", "updated_at")

    def get_preview_image(self, obj) -> int | None:
        membership = obj.memberships.exclude(state__in=[IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED]).select_related("identity_face").first()
        return membership.identity_face.image_id if membership else None


class IdentityMembershipSerializer(serializers.ModelSerializer):
    image = serializers.IntegerField(source="identity_face.image_id", read_only=True)
    face_index = serializers.IntegerField(source="identity_face.face_index", read_only=True)

    class Meta:
        model = IdentityMembership
        fields = ("id", "image", "face_index", "identity_group", "similarity_score", "matching_distance", "state", "decision_method", "evidence", "reviewed_at", "created_at", "updated_at")


class IdentityReviewRequestSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("confirm", "select_group", "new_group", "reject"))
    group_id = serializers.IntegerField(required=False, min_value=1)

    def validate(self, attrs):
        if attrs["action"] == "select_group" and "group_id" not in attrs:
            raise serializers.ValidationError({"group_id": "This field is required when selecting a group."})
        return attrs


class IdentityRenameSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=80)


class IdentityMergeSerializer(serializers.Serializer):
    source_group_id = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")


class IdentityMoveSerializer(serializers.Serializer):
    target_group_id = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")


class IdentityReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")


class IdentityProfileSerializer(serializers.Serializer):
    membership_id = serializers.IntegerField(min_value=1)


class IdentityChangeSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityChange
        fields = ("id", "action", "group", "membership", "before", "after", "reason", "created_at")


class IdentityGroupExportRequestSerializer(serializers.Serializer):
    include_originals = serializers.BooleanField(default=True)
    include_preprocessed = serializers.BooleanField(default=False)
    include_enhanced = serializers.BooleanField(default=True)
    include_report = serializers.BooleanField(default=True)
    report_format = serializers.ChoiceField(
        choices=IdentityGroupExport.ReportFormat.choices,
        default=IdentityGroupExport.ReportFormat.JSON,
    )

    def validate(self, attrs):
        if not any(attrs[key] for key in (
            "include_originals", "include_preprocessed", "include_enhanced",
            "include_report",
        )):
            raise serializers.ValidationError(
                "Select at least one export component."
            )
        return attrs


class IdentityGroupExportSerializer(serializers.ModelSerializer):
    group = serializers.IntegerField(source="identity_group_id", read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = IdentityGroupExport
        fields = (
            "id", "group", "include_originals", "include_preprocessed",
            "include_enhanced", "include_report", "report_format", "status",
            "progress_percentage", "display_name", "manifest", "file_size",
            "failure_reason", "download_url", "generated_at", "expires_at",
            "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_download_url(self, obj) -> str | None:
        if obj.status != IdentityGroupExport.Status.READY or not obj.export_file:
            return None
        request = self.context.get("request")
        path = f"/api/identities/exports/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path

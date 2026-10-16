from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from uploads.models import ImageUpload

from .models import (IdentityAnalysis, IdentityChange, IdentityGroup,
                     IdentityGroupExport, IdentityMembership)


class IdentityAnalyzeRequestSerializer(serializers.Serializer):
    force = serializers.BooleanField(default=False, required=False)


class IdentityAnalysisSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityAnalysis
        fields = ("id", "image", "status", "face_count", "assigned_count", "review_count", "provider", "policy_version", "failure_reason", "processing_duration_ms", "started_at", "finished_at", "created_at", "updated_at")


INACTIVE_MEMBERSHIP_STATES = (IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED)
COVER_IMAGE_LIMIT = 4


def active_memberships_of(group):
    """The group's members, from the list view's prefetch when it ran, else one query."""
    prefetched = getattr(group, "active_memberships", None)
    if prefetched is not None:
        return prefetched
    return list(
        group.memberships.exclude(state__in=INACTIVE_MEMBERSHIP_STATES).select_related("identity_face__image")
    )


class CoverImageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    thumbnail_url = serializers.CharField()


class IdentityGroupSerializer(serializers.ModelSerializer):
    preview_image = serializers.SerializerMethodField()
    cover_images = serializers.SerializerMethodField()
    matching_confidence = serializers.SerializerMethodField()

    class Meta:
        model = IdentityGroup
        fields = (
            "id", "display_name", "state", "member_count", "preview_image",
            "cover_images", "matching_confidence", "created_at", "updated_at",
        )

    def get_preview_image(self, obj) -> int | None:
        members = active_memberships_of(obj)
        return members[0].identity_face.image_id if members else None

    @extend_schema_field(CoverImageSerializer(many=True))
    def get_cover_images(self, obj):
        """Up to four distinct photos of the folder, each with its thumbnail address."""
        request = self.context.get("request")
        covers, seen = [], set()
        for member in active_memberships_of(obj):
            image = member.identity_face.image
            if image.pk in seen or image.status != "uploaded" or not image.original_file:
                continue
            seen.add(image.pk)
            path = f"/api/images/{image.pk}/thumbnail/"
            covers.append({"id": image.pk, "thumbnail_url": request.build_absolute_uri(path) if request else path})
            if len(covers) == COVER_IMAGE_LIMIT:
                break
        return covers

    def get_matching_confidence(self, obj) -> float | None:
        """Average member similarity as a percentage (0-100, one decimal); null when no member has a score."""
        scores = [m.similarity_score for m in active_memberships_of(obj) if m.similarity_score is not None]
        return round(100 * sum(scores) / len(scores), 1) if scores else None


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


class IdentitySearchPhotoSerializer(serializers.ModelSerializer):
    thumbnail_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = ("id", "original_name", "thumbnail_url", "download_url")

    def _absolute(self, path):
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_thumbnail_url(self, obj) -> str:
        return self._absolute(f"/api/images/{obj.pk}/thumbnail/")

    def get_download_url(self, obj) -> str:
        return self._absolute(f"/api/images/{obj.pk}/download/")


class IdentitySearchMatchSerializer(serializers.Serializer):
    group_id = serializers.IntegerField()
    display_name = serializers.CharField()
    similarity = serializers.FloatField()
    photo_count = serializers.IntegerField()
    photos = IdentitySearchPhotoSerializer(many=True)


class IdentitySearchResponseSerializer(serializers.Serializer):
    faces_detected = serializers.BooleanField(help_text="Whether a face was found in the query photo at all.")
    matches = IdentitySearchMatchSerializer(many=True)

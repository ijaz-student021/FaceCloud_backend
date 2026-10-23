from django.conf import settings
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
        fields = ("id", "image", "status", "face_count", "assigned_count", "review_count", "provider", "policy_version", "failure_reason", "evidence", "processing_duration_ms", "started_at", "finished_at", "created_at", "updated_at")


INACTIVE_MEMBERSHIP_STATES = (IdentityMembership.State.REJECTED, IdentityMembership.State.REMOVED)
CONFIRMED_STATES = (IdentityMembership.State.AUTO_MATCHED, IdentityMembership.State.NEW_GROUP, IdentityMembership.State.CONFIRMED)
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


def avatar_path(group_id):
    return f"/api/identities/groups/{group_id}/avatar/"


class PersonChipSerializer(serializers.Serializer):
    """One person as a chip: a round face, a name and how many photographs."""

    id = serializers.IntegerField()
    display_name = serializers.CharField()
    is_named = serializers.SerializerMethodField(help_text="False while the folder still has its automatic name (Person 001): show it as 'Add a name'.")
    photo_count = serializers.IntegerField(help_text="Distinct photographs of this person (faces placed in the folder).")
    avatar_url = serializers.SerializerMethodField(help_text="A square crop of the person's best face: GET it with the Authorization header, like a thumbnail. ?size=96, 192 or 384.")

    def get_is_named(self, obj) -> bool:
        from .people import is_named

        return is_named(obj)

    def get_avatar_url(self, obj) -> str:
        request = self.context.get("request")
        path = avatar_path(obj.pk)
        return request.build_absolute_uri(path) if request else path


class IdentityGroupSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField(help_text="A square crop of the person's best face (null while no face is placed in the folder). See GET /api/identities/groups/{id}/avatar/.")
    preview_image = serializers.SerializerMethodField()
    cover_images = serializers.SerializerMethodField()
    matching_confidence = serializers.SerializerMethodField()
    confirmed_count = serializers.SerializerMethodField(help_text=(
        "Faces placed in this folder (the same count as member_count): matched automatically, a new folder's first face, or confirmed by the owner."))
    suggested_count = serializers.SerializerMethodField(help_text=(
        "Faces that look like this person but are waiting for a decision (needs_review). They are listed with the folder's photos but are not part of member_count."))

    class Meta:
        model = IdentityGroup
        fields = (
            "id", "display_name", "state", "member_count", "confirmed_count", "suggested_count", "avatar_url", "preview_image",
            "cover_images", "matching_confidence", "created_at", "updated_at",
        )

    def get_avatar_url(self, obj) -> str | None:
        if not any(member.state in CONFIRMED_STATES for member in active_memberships_of(obj)):
            return None
        request = self.context.get("request")
        path = avatar_path(obj.pk)
        return request.build_absolute_uri(path) if request else path

    def get_confirmed_count(self, obj) -> int:
        return sum(1 for member in active_memberships_of(obj) if member.state in CONFIRMED_STATES)

    def get_suggested_count(self, obj) -> int:
        return sum(1 for member in active_memberships_of(obj) if member.state == IdentityMembership.State.NEEDS_REVIEW)

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


class SuggestedMergeFolderSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    display_name = serializers.CharField()
    member_count = serializers.IntegerField()


class SuggestedMergeSerializer(serializers.Serializer):
    """Two of the owner's own folders that look like one person. Merge with ``POST /api/identities/groups/{target_group}/merge/`` and ``{"source_group_id": source_group}``."""

    source_group = serializers.IntegerField(help_text="The folder to merge away: the one with fewer photographs (the newer on a tie).")
    target_group = serializers.IntegerField(help_text="The folder to keep.")
    similarity = serializers.FloatField(help_text="How alike their average faces are, 0 to 1 (cosine similarity).")
    source = SuggestedMergeFolderSerializer()
    target = SuggestedMergeFolderSerializer()


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
    image_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_null=True,
        allow_empty=False, max_length=1000,
        help_text="Optional subset of this folder's photos to export. Omit it to export everything. "
                  "An id that is not in the folder is a 400.",
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


class IdentityExportShareRequestSerializer(serializers.Serializer):
    ttl_hours = serializers.IntegerField(
        required=False, min_value=1,
        help_text="How long the link works. Defaults to SHARE_LINK_DEFAULT_TTL_HOURS and never outlasts the ZIP.",
    )
    label = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate_ttl_hours(self, value):
        if value > settings.SHARE_LINK_MAX_TTL_HOURS:
            raise serializers.ValidationError(f"A link can last at most {settings.SHARE_LINK_MAX_TTL_HOURS} hours.")
        return value


class IdentityGroupExportSerializer(serializers.ModelSerializer):
    group = serializers.IntegerField(source="identity_group_id", read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = IdentityGroupExport
        fields = (
            "id", "group", "include_originals", "include_preprocessed",
            "include_enhanced", "include_report", "report_format", "image_ids", "status",
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

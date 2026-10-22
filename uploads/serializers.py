from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from .display_status import image_display_status, latest_result
from .models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult
from quality_control.models import ImageQualityDecision
from duplicates.models import DuplicateAnalysis
from duplicates.serializers import DuplicateAnalysisSerializer
from enhancement.serializers import ImageEnhancementResultSerializer
from identities.serializers import IdentityAnalysisSerializer
from quality_control.serializers import ImageQualityDecisionSerializer
from validation.serializers import FaceValidationResultSerializer


class ImagePreprocessingResultSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImagePreprocessingResult
        fields = [
            "id", "image", "status", "requested_format", "requested_name",
            "original_format", "output_format", "output_name", "original_width",
            "original_height", "output_width", "output_height", "original_file_size",
            "processed_file_size", "had_transparency", "processing_duration_ms",
            "failure_reason", "download_url", "created_at", "updated_at",
        ]

    def get_download_url(self, obj) -> str | None:
        if obj.status != "completed" or not obj.processed_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/{obj.image_id}/preprocessing/download/"
        return request.build_absolute_uri(path) if request else path


class PreprocessImageRequestSerializer(serializers.Serializer):
    output_format = serializers.ChoiceField(choices=["auto", "png", "jpeg"], default="auto")
    output_name = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    force = serializers.BooleanField(required=False, default=False)


class ImageUploadSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    preprocessing = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = [
            "id", "batch", "original_name", "file_extension", "mime_type",
            "file_size", "width", "height", "checksum_sha256", "status",
            "rejection_reason", "download_url", "thumbnail_url", "preprocessing", "is_favorite", "created_at",
        ]

    def get_download_url(self, obj) -> str | None:
        if obj.status != "uploaded" or not obj.original_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/{obj.pk}/download/"
        return request.build_absolute_uri(path) if request else path

    def get_thumbnail_url(self, obj) -> str | None:
        if obj.status != "uploaded" or not obj.original_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/{obj.pk}/thumbnail/"
        return request.build_absolute_uri(path) if request else path

    @extend_schema_field(ImagePreprocessingResultSerializer(allow_null=True))
    def get_preprocessing(self, obj):
        try:
            result = obj.preprocessing_result
        except ImagePreprocessingResult.DoesNotExist:
            return None
        return ImagePreprocessingResultSerializer(result, context=self.context).data


IDENTITY_GROUP_REFERENCES = inline_serializer(
    "IdentityGroupReference",
    {"id": serializers.IntegerField(), "display_name": serializers.CharField()},
    many=True,
)


class MyImageSerializer(ImageUploadSerializer):
    batch_display_name = serializers.CharField(source="batch.display_name", read_only=True)
    upload_type = serializers.CharField(source="batch.upload_type", read_only=True)
    display_status = serializers.SerializerMethodField()
    latest_enhancement = serializers.SerializerMethodField()
    face_validation = serializers.SerializerMethodField()
    quality_decision = serializers.SerializerMethodField()
    duplicate_analysis = serializers.SerializerMethodField()
    identity_analysis = serializers.SerializerMethodField()
    identity_groups = serializers.SerializerMethodField()

    class Meta(ImageUploadSerializer.Meta):
        fields = ImageUploadSerializer.Meta.fields + [
            "batch_display_name", "upload_type", "display_status",
            "latest_enhancement", "face_validation", "quality_decision",
            "duplicate_analysis", "identity_analysis", "identity_groups", "updated_at",
        ]

    @staticmethod
    def _latest_result(obj):
        return latest_result(obj)

    @extend_schema_field(ImageEnhancementResultSerializer(allow_null=True))
    def get_latest_enhancement(self, obj):
        result = self._latest_result(obj)
        if result is None:
            return None
        return ImageEnhancementResultSerializer(result, context=self.context).data

    @extend_schema_field(FaceValidationResultSerializer(allow_null=True))
    def get_face_validation(self, obj):
        try:
            result = obj.face_validation
        except FaceValidationResult.DoesNotExist:
            return None
        return FaceValidationResultSerializer(result).data

    @extend_schema_field(ImageQualityDecisionSerializer(allow_null=True))
    def get_quality_decision(self, obj):
        try:
            result = obj.quality_decision
        except ImageQualityDecision.DoesNotExist:
            return None
        return ImageQualityDecisionSerializer(result).data

    @extend_schema_field(DuplicateAnalysisSerializer(allow_null=True))
    def get_duplicate_analysis(self, obj):
        try:
            result = obj.duplicate_analysis
        except DuplicateAnalysis.DoesNotExist:
            return None
        return DuplicateAnalysisSerializer(result, context=self.context).data

    @extend_schema_field(IdentityAnalysisSerializer(allow_null=True))
    def get_identity_analysis(self, obj):
        try:
            result = obj.identity_analysis
        except Exception:
            return None
        return IdentityAnalysisSerializer(result).data

    @extend_schema_field(IDENTITY_GROUP_REFERENCES)
    def get_identity_groups(self, obj):
        faces = getattr(obj, "prefetched_identity_faces", None)
        if faces is not None:
            # Prefetched by MyImageListView: no query per image.
            groups = {}
            for face in faces:
                for membership in face.active_memberships:
                    group = membership.identity_group
                    if group is not None:
                        groups.setdefault(group.pk, {"id": group.pk, "display_name": group.display_name})
            return list(groups.values())
        # order_by() clears the model ordering, which would otherwise leak into DISTINCT.
        memberships = obj.identity_faces.filter(memberships__state__in=["auto_matched", "new_group", "confirmed"]).order_by().values("memberships__identity_group_id", "memberships__identity_group__display_name").distinct()
        return [{"id": row["memberships__identity_group_id"], "display_name": row["memberships__identity_group__display_name"]} for row in memberships if row["memberships__identity_group_id"]]

    def get_display_status(self, obj) -> str:
        return image_display_status(obj)


ENHANCEMENT_STAGE = inline_serializer("EnhancementStage", {
    "status": serializers.CharField(help_text="queued, processing, completed, skipped, failed or not_applicable."),
    "outcome": serializers.CharField(allow_null=True, help_text="enhanced, partially_enhanced, original_preserved or failed; null until the enhancement has ended."),
    "summary": serializers.CharField(help_text="One plain sentence on the outcome, safe to show a person; empty until it has ended."),
    "waiting_for": serializers.CharField(help_text="While queued and not yet started: quality_verdict, moderator_approval or capacity; otherwise empty."),
    "skipped_reason": serializers.CharField(help_text="When skipped: capacity, cancelled, already_requested, not_possible or error; otherwise empty."),
})


class BatchImageSerializer(ImageUploadSerializer):
    """A photo inside a batch response: the upload facts plus the same status word My Images shows, and its enhancement stage."""

    display_status = serializers.SerializerMethodField()
    enhancement = serializers.SerializerMethodField(help_text=(
        "The automatic enhancement of this photograph: where it is (status, waiting_for) and how it ended (outcome, summary). 'not_applicable' when the upload "
        "turned it off or the photograph was rejected by the checks."
    ))

    class Meta(ImageUploadSerializer.Meta):
        fields = ImageUploadSerializer.Meta.fields + ["display_status", "enhancement"]

    def get_display_status(self, obj) -> str:
        return image_display_status(obj)

    @extend_schema_field(ENHANCEMENT_STAGE)
    def get_enhancement(self, obj):
        from enhancement.auto import enhancement_state

        return enhancement_state(obj)


class UploadBatchSerializer(serializers.ModelSerializer):
    images = BatchImageSerializer(many=True, read_only=True)

    class Meta:
        model = UploadBatch
        fields = [
            "id", "upload_type", "original_archive_name", "display_name", "status", "total_files", "auto_enhance",
            "accepted_files", "rejected_files", "total_size", "processing_status",
            "processed_files", "processing_failed_files", "processing_task_id",
            "processing_error", "processing_started_at", "processing_finished_at",
            "created_at", "updated_at", "images",
        ]
        read_only_fields = [
            "upload_type", "original_archive_name", "status", "total_files", "auto_enhance", "accepted_files",
            "rejected_files", "total_size", "processing_status", "processed_files",
            "processing_failed_files", "processing_task_id", "processing_error",
            "processing_started_at", "processing_finished_at", "created_at", "updated_at", "images",
        ]

    def validate_display_name(self, value):
        return " ".join(value.split()).strip()


class UploadBatchCreatedSerializer(UploadBatchSerializer):
    """The answer to an upload: the batch, and whether its jobs run inside this request."""

    processing_mode = serializers.SerializerMethodField(
        help_text="'inline': background jobs run inside this request (CELERY_TASK_ALWAYS_EAGER is on, normally development), so the batch is already processed "
                  "and there is no progress to show. 'background': a Celery worker does the work and the progress endpoints move.",
    )

    class Meta(UploadBatchSerializer.Meta):
        fields = UploadBatchSerializer.Meta.fields + ["processing_mode"]
        read_only_fields = UploadBatchSerializer.Meta.read_only_fields + ["processing_mode"]

    def get_processing_mode(self, batch) -> str:
        from operations.inline import processing_mode

        return processing_mode()

from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from . import comparison as comparison_engine
from . import watermark as wm
from .controls import INTENTS, ControlsSerializer
from .models import (
    AdvisorRecommendation,
    AdvisorRun,
    EditMode,
    EditPreview,
    EnhancementBatchJob,
    EnhancementLevel,
    EnhancementStatus,
    EnhancementStyle,
    ImageEditVersion,
    ImageEnhancementResult,
    ModelComparison,
    ModelComparisonOutput,
)


class EnhancementRequestSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=EnhancementLevel.choices, default=EnhancementLevel.AUTO)
    style = serializers.ChoiceField(
        choices=EnhancementStyle.choices, default=EnhancementStyle.NATURAL,
        help_text=(
            "`natural` and `dynamic` restore faces only when the result can be checked against "
            "the original. `ai_max` skips that requirement: it may restore faces without a "
            "verified identity check. The result says so in `identity_check_bypassed` and "
            "`identity_notice`."
        ),
    )


class WatermarkOptionsSerializer(serializers.Serializer):
    """Query parameters shared by the download and preview endpoints."""

    watermark = serializers.ChoiceField(
        choices=wm.MODES, default="none",
        help_text="none, small (small FaceCloud mark), facecloud, username or custom.",
    )
    watermark_position = serializers.ChoiceField(choices=wm.POSITIONS, default="bottom_right")
    watermark_opacity = serializers.FloatField(
        min_value=wm.MIN_OPACITY, max_value=wm.MAX_OPACITY,
        default=wm.DEFAULT_OPACITY,
    )
    watermark_text = serializers.CharField(
        required=False, allow_blank=True, max_length=wm.MAX_CUSTOM_LENGTH,
        help_text="Only used when watermark=custom.",
    )

    def validate(self, attrs):
        try:
            attrs["spec"] = wm.validate_spec(
                mode=attrs["watermark"], position=attrs["watermark_position"],
                opacity=attrs["watermark_opacity"], text=attrs.get("watermark_text", ""),
            )
        except wm.WatermarkError as error:
            raise serializers.ValidationError({error.field: [str(error)]}) from error
        return attrs


class GalleryAdjustmentSerializer(ControlsSerializer):
    """Manual control values: signed numbers, 0 neutral (``intensity``: 0-100, 100 neutral).

    Finite numbers only; booleans, NaN, infinity, out-of-range values and unknown
    control names are field errors. See ``GET /api/enhancements/editor/configuration/``
    for each control's range, meaning and Pillow-factor mapping.
    """


class GalleryEditRequestSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=EditMode.choices, default=EditMode.AUTO)
    adjustments = GalleryAdjustmentSerializer(
        required=False, default=dict,
        help_text="Used in manual mode only. Omitted or empty means neutral (the edit changes nothing).",
    )
    intent = serializers.ChoiceField(
        choices=INTENTS, default="auto",
        help_text=(
            "Auto mode only. auto: fix clear technical faults only (never lifts or darkens overall "
            "exposure); brighten: also lift a dark photo; preserve: leave tonality alone."
        ),
    )

    source_version = serializers.CharField(
        required=False, allow_blank=True, max_length=32,
        help_text="Optional: the source_version this edit is meant for; a 409 stale_source if it is no longer current.",
    )

    def validate(self, attrs):
        if attrs["mode"] == EditMode.AUTO:
            attrs["adjustments"] = {"intent": attrs["intent"]}
        elif self.initial_data.get("intent") not in (None, "auto"):
            raise serializers.ValidationError({"intent": "Intent applies to auto mode only."})
        return attrs


IDENTITY_STATUS_HELP = (
    "`verified`: faces in the result were compared with the original and matched. "
    "`inconclusive`: no reliable comparison was possible. `unavailable`: no check ran "
    "(no consent, no identity model, or switched off). `failed`: compared and did not "
    "match. Null when nothing was enhanced, and for records made before this field existed."
)
IDENTITY_BYPASSED_HELP = (
    "True when faces were generatively restored without a verified identity check, "
    "which only AI Max (or an operator setting) allows. Show `identity_notice` to the user."
)
IDENTITY_NOTICE_HELP = (
    "A sentence for the user when identity was not verified under a bypass; otherwise empty."
)


def identity_fields(summary, legacy_status=None):
    """``(identity_status, identity_check_bypassed, identity_notice)`` from a stored summary.

    One definition for every endpoint that reports on an enhanced image, so the
    same result cannot read as verified in one place and unverified in another.
    """
    summary = summary if isinstance(summary, dict) else {}
    return (
        summary.get("status") or legacy_status or None,
        bool(summary.get("face_restoration_unverified")),
        str(summary.get("notice") or ""),
    )


class ImageEditVersionSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ImageEditVersion
        fields = [
            "id", "enhancement_result", "mode", "requested_adjustments",
            "applied_adjustments", "status", "output_name", "output_format",
            "output_width", "output_height", "output_file_size",
            "processing_duration_ms", "task_id", "quality_metadata",
            "failure_reason", "is_current", "download_url", "started_at",
            "finished_at", "created_at", "updated_at", "source_version", "from_preview",
            "advisor_recommendation",
        ]

    def get_download_url(self, obj) -> str | None:
        if obj.status != EnhancementStatus.COMPLETED or not obj.edited_file:
            return None
        path = f"/api/enhancements/edits/{obj.pk}/download/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class ImageEnhancementResultSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    original_download_url = serializers.SerializerMethodField()
    standardized_download_url = serializers.SerializerMethodField()
    editor_url = serializers.SerializerMethodField()
    current_edit = serializers.SerializerMethodField()
    identity_status = serializers.SerializerMethodField(help_text=IDENTITY_STATUS_HELP)
    identity_check_bypassed = serializers.SerializerMethodField(help_text=IDENTITY_BYPASSED_HELP)
    identity_notice = serializers.SerializerMethodField(help_text=IDENTITY_NOTICE_HELP)
    enhancement_outcome = serializers.SerializerMethodField(help_text=(
        "What the adaptive engine did: enhanced, partially_enhanced or original_preserved (the output is the original "
        "pixels); failed when the job failed. null for results made by the staged pipeline, which does not classify."))
    enhancement_summary = serializers.SerializerMethodField(help_text="One plain sentence on the outcome, safe to show a user.")

    class Meta:
        model = ImageEnhancementResult
        fields = [
            "id", "image", "batch_job", "requested_level", "applied_level",
            "requested_style", "status", "enhancement_outcome", "enhancement_summary",
            "identity_status", "identity_check_bypassed", "identity_notice",
            "output_name", "output_format", "output_width", "output_height",
            "output_file_size", "processing_duration_ms", "task_id", "applied_parameters",
            "failure_reason", "original_download_url", "standardized_download_url",
            "download_url", "editor_url", "current_edit", "started_at",
            "finished_at", "created_at",
        ]

    def _absolute(self, path):
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def _identity(self, obj):
        parameters = obj.applied_parameters if isinstance(obj.applied_parameters, dict) else {}
        return identity_fields(parameters.get("identity_verification"), parameters.get("identity_status"))

    def get_enhancement_outcome(self, obj) -> str | None:
        if obj.status == "failed":
            return "failed"
        parameters = obj.applied_parameters if isinstance(obj.applied_parameters, dict) else {}
        adaptive = parameters.get("adaptive")
        return adaptive.get("outcome") if isinstance(adaptive, dict) else None

    def get_enhancement_summary(self, obj) -> str:
        if obj.status == "failed":
            return "The photograph could not be enhanced. Your original photograph is unchanged."
        parameters = obj.applied_parameters if isinstance(obj.applied_parameters, dict) else {}
        adaptive = parameters.get("adaptive")
        return str(adaptive.get("summary", "")) if isinstance(adaptive, dict) else ""

    def get_identity_status(self, obj) -> str | None:
        return self._identity(obj)[0]

    def get_identity_check_bypassed(self, obj) -> bool:
        return self._identity(obj)[1]

    def get_identity_notice(self, obj) -> str:
        return self._identity(obj)[2]

    def get_original_download_url(self, obj) -> str:
        return self._absolute(f"/api/images/{obj.image_id}/download/")

    def get_standardized_download_url(self, obj) -> str:
        return self._absolute(f"/api/images/{obj.image_id}/preprocessing/download/")

    def get_download_url(self, obj) -> str | None:
        if obj.status != "completed" or not obj.enhanced_file:
            return None
        path = f"/api/enhancements/results/{obj.pk}/download/"
        return self._absolute(path)

    def get_editor_url(self, obj) -> str | None:
        if obj.status != EnhancementStatus.COMPLETED or not obj.enhanced_file:
            return None
        return self._absolute(f"/api/enhancements/results/{obj.pk}/edits/")

    @extend_schema_field(ImageEditVersionSerializer(allow_null=True))
    def get_current_edit(self, obj):
        current = next(
            (edit for edit in obj.edit_versions.all() if edit.is_current), None
        )
        if current is None:
            return None
        return ImageEditVersionSerializer(
            current, context=self.context
        ).data


def _file_summary(name, extra=()):
    fields = {
        "filename": serializers.CharField(),
        "format": serializers.CharField(),
        "width": serializers.IntegerField(allow_null=True),
        "height": serializers.IntegerField(allow_null=True),
        "file_size": serializers.IntegerField(),
        "download_url": serializers.CharField(),
    }
    fields.update({key: serializers.IntegerField() for key in extra})
    return inline_serializer(name, fields)


ORIGINAL_FILE_SUMMARY = _file_summary("ComparisonOriginalFile")
STANDARDIZED_FILE_SUMMARY = _file_summary(
    "ComparisonStandardizedFile", extra=("processing_duration_ms",)
)
ENHANCED_FILE_SUMMARY = _file_summary("ComparisonEnhancedFile")


_METRIC_CHANGE = inline_serializer("ComparisonMetricChange", {
    "before": serializers.FloatField(allow_null=True),
    "after": serializers.FloatField(allow_null=True),
    "change": serializers.FloatField(allow_null=True),
})


QUALITY_COMPARISON = inline_serializer("ComparisonQuality", {
    "method": serializers.CharField(),
    "reference_dimensions": serializers.ListField(child=serializers.IntegerField()),
    "metrics": inline_serializer("ComparisonMetrics", {
        name: _METRIC_CHANGE
        for name in (
            "brightness", "contrast", "sharpness", "noise",
            "shadow_clipping", "highlight_clipping",
        )
    }),
})


class ImageEnhancementComparisonSerializer(serializers.Serializer):
    """Public, owner-scoped comparison contract for a completed result."""

    METRICS = {
        "brightness": "mean_luminance",
        "contrast": "contrast_stddev",
        "sharpness": "laplacian_sharpness",
        "noise": "estimated_noise",
        "shadow_clipping": "shadow_clipping_percent",
        "highlight_clipping": "highlight_clipping_percent",
    }

    result_id = serializers.IntegerField(source="pk", read_only=True)
    image_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    requested_level = serializers.CharField(read_only=True)
    applied_level = serializers.CharField(read_only=True)
    algorithm = serializers.SerializerMethodField()
    processing_duration_ms = serializers.IntegerField(read_only=True)
    original = serializers.SerializerMethodField()
    standardized = serializers.SerializerMethodField()
    enhanced = serializers.SerializerMethodField()
    comparison = serializers.SerializerMethodField()
    warnings = serializers.SerializerMethodField()

    def _absolute(self, path):
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_algorithm(self, obj) -> str:
        return obj.applied_parameters.get("algorithm", "")

    @extend_schema_field(ORIGINAL_FILE_SUMMARY)
    def get_original(self, obj):
        image = obj.image
        return {
            "filename": image.original_name,
            "format": image.file_extension.lstrip(".").lower(),
            "width": image.width,
            "height": image.height,
            "file_size": image.file_size,
            "download_url": self._absolute(f"/api/images/{image.pk}/download/"),
        }

    @extend_schema_field(STANDARDIZED_FILE_SUMMARY)
    def get_standardized(self, obj):
        result = obj.preprocessing_result
        return {
            "filename": result.output_name,
            "format": result.output_format,
            "width": result.output_width,
            "height": result.output_height,
            "file_size": result.processed_file_size,
            "processing_duration_ms": result.processing_duration_ms,
            "download_url": self._absolute(
                f"/api/images/{obj.image_id}/preprocessing/download/"
            ),
        }

    @extend_schema_field(ENHANCED_FILE_SUMMARY)
    def get_enhanced(self, obj):
        return {
            "filename": obj.output_name,
            "format": obj.output_format,
            "width": obj.output_width,
            "height": obj.output_height,
            "file_size": obj.output_file_size,
            "download_url": self._absolute(
                f"/api/enhancements/results/{obj.pk}/download/"
            ),
        }

    @extend_schema_field(QUALITY_COMPARISON)
    def get_comparison(self, obj):
        quality = obj.applied_parameters.get("quality_comparison", {})
        before = quality.get("before", {})
        after = quality.get("after", {})
        deltas = quality.get("deltas", {})
        metrics = {}
        for public_name, stored_name in self.METRICS.items():
            metrics[public_name] = {
                "before": before.get(stored_name),
                "after": after.get(stored_name),
                "change": deltas.get(stored_name),
            }
        return {
            "method": quality.get("method", ""),
            "reference_dimensions": quality.get("reference_dimensions", []),
            "metrics": metrics,
        }

    def get_warnings(self, obj) -> list[str]:
        warnings = obj.applied_parameters.get("warnings", [])
        if not isinstance(warnings, list):
            return []
        return [str(warning) for warning in warnings]


class EnhancementBatchJobSerializer(serializers.ModelSerializer):
    progress_percentage = serializers.FloatField(read_only=True)
    results = ImageEnhancementResultSerializer(many=True, read_only=True)

    class Meta:
        model = EnhancementBatchJob
        fields = [
            "id", "source_batch", "requested_level", "requested_style", "status", "total_images",
            "processed_images", "successful_images", "failed_images",
            "progress_percentage", "failure_reason", "started_at", "finished_at",
            "created_at", "updated_at", "results",
        ]


class ModelComparisonRequestSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=EnhancementLevel.choices, default=EnhancementLevel.AUTO)
    profiles = serializers.ListField(
        child=serializers.ChoiceField(choices=list(comparison_engine.PROFILES)),
        required=False, allow_empty=True,
        help_text="Profiles to compare; omit for all: " + ", ".join(comparison_engine.PROFILES) + ".",
    )


class ModelComparisonOutputSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    identity_status = serializers.SerializerMethodField(help_text=IDENTITY_STATUS_HELP)
    identity_check_bypassed = serializers.SerializerMethodField(help_text=IDENTITY_BYPASSED_HELP)
    identity_notice = serializers.SerializerMethodField(help_text=IDENTITY_NOTICE_HELP)

    class Meta:
        model = ModelComparisonOutput
        fields = [
            "profile", "label", "description", "outcome", "preview_url", "algorithm",
            "overall_score", "scores", "metrics", "faces_detected", "identity_guard",
            "identity_status", "identity_check_bypassed", "identity_notice",
            "warnings", "processing_duration_ms", "failure_reason",
        ]
        read_only_fields = fields

    def _identity(self, obj):
        guard = obj.identity_guard if isinstance(obj.identity_guard, dict) else {}
        return identity_fields(guard.get("verification"))

    def get_identity_status(self, obj) -> str | None:
        return self._identity(obj)[0]

    def get_identity_check_bypassed(self, obj) -> bool:
        return self._identity(obj)[1]

    def get_identity_notice(self, obj) -> str:
        return self._identity(obj)[2]

    def _profile(self, obj):
        return comparison_engine.PROFILES.get(obj.profile)

    def get_label(self, obj) -> str:
        profile = self._profile(obj)
        if profile:
            return profile.label
        return "Original (untouched)" if obj.profile == comparison_engine.ORIGINAL else obj.profile

    def get_description(self, obj) -> str:
        profile = self._profile(obj)
        return profile.description if profile else "The source image at the comparison size."

    def get_preview_url(self, obj) -> str | None:
        if not obj.preview_file:
            return None
        path = f"/api/enhancements/comparisons/{obj.comparison_id}/outputs/{obj.profile}/preview/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class ModelComparisonSummarySerializer(serializers.ModelSerializer):
    image = serializers.IntegerField(source="image_id", read_only=True)

    class Meta:
        model = ModelComparison
        fields = [
            "id", "image", "requested_level", "profiles", "status", "reference_dimensions",
            "recommended_profile", "recommendation_reason", "total_duration_ms",
            "failure_reason", "started_at", "finished_at", "created_at",
        ]
        read_only_fields = fields


class ModelComparisonSerializer(ModelComparisonSummarySerializer):
    outputs = ModelComparisonOutputSerializer(many=True, read_only=True)
    report_url = serializers.SerializerMethodField()

    class Meta(ModelComparisonSummarySerializer.Meta):
        fields = ModelComparisonSummarySerializer.Meta.fields + ["outputs", "report_url"]
        read_only_fields = fields

    def get_report_url(self, obj) -> str | None:
        if obj.status != EnhancementStatus.COMPLETED:
            return None
        path = f"/api/enhancements/comparisons/{obj.pk}/report/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class EditorControlSpecSerializer(serializers.Serializer):
    label = serializers.CharField()
    minimum = serializers.FloatField(help_text="Lowest value a person may set.")
    maximum = serializers.FloatField(help_text="Highest value a person may set.")
    step = serializers.FloatField()
    default = serializers.FloatField(help_text="The value Reset sets; the same as neutral.")
    neutral = serializers.FloatField(help_text="The value that changes nothing.")
    recommended_minimum = serializers.FloatField()
    recommended_maximum = serializers.FloatField()
    auto_minimum = serializers.FloatField(help_text="Lowest value the local recipe or the cloud advisor may set.")
    auto_maximum = serializers.FloatField(help_text="Highest value the local recipe or the cloud advisor may set.")
    description = serializers.CharField(help_text="Exact renderer semantics.")
    pillow_enhancer = serializers.CharField(
        allow_null=True, help_text="The Pillow ImageEnhance class this approximates; null if none.",
    )
    pillow_factor_per_unit = serializers.FloatField(
        allow_null=True,
        help_text="Approximate Pillow factor = 1 + this * value (Pillow's neutral is 1.0, ours is 0).",
    )


class EditorConfigurationSerializer(serializers.Serializer):
    modes = serializers.ListField(child=serializers.CharField())
    source = serializers.CharField()
    non_destructive = serializers.BooleanField()
    preview_strategy = serializers.CharField()
    renderer_version = serializers.CharField(help_text="Recorded on every saved edit.")
    preview = serializers.DictField(help_text="Preview bounds, retention and revision rules.")
    transparency = serializers.DictField(help_text="How alpha and JPEG compositing are handled.")
    render_policy = serializers.DictField(help_text="The active rendering policy (what may run).")
    intents = serializers.ListField(child=serializers.CharField(), help_text="Auto-mode intents.")
    neutral_settings = serializers.DictField(
        child=serializers.FloatField(), help_text="What Reset sets: a render with these values changes nothing.",
    )
    controls = serializers.DictField(child=EditorControlSpecSerializer())


class PreviewRequestSerializer(GalleryEditRequestSerializer):
    """A preview request: the same settings as a saved edit, plus the revision and source it is for."""

    revision = serializers.IntegerField(
        min_value=1, max_value=2_147_483_647,
        help_text=(
            "Chosen by the client, increasing with every change it wants to see. The highest revision is the "
            "latest; an answer to an older one is stored but reported with is_latest false, and never replaces a "
            "newer preview. Repeating a revision with the same settings returns the stored preview; with different "
            "settings it is a 409."
        ),
    )
    source_version = serializers.CharField(
        required=False, allow_blank=True, max_length=32,
        help_text="The source_version this request was made against; a 409 stale_source if it is no longer current.",
    )
    size = serializers.IntegerField(
        required=False, min_value=1, max_value=65_535,
        help_text="Longer edge in pixels (default and maximum are in the editor configuration).",
    )


class PreviewSourceSerializer(serializers.Serializer):
    width = serializers.IntegerField()
    height = serializers.IntegerField()
    full_width = serializers.IntegerField(help_text="Size of the full-resolution image a save renders.")
    full_height = serializers.IntegerField()
    has_alpha = serializers.BooleanField()


class EditPreviewSerializer(serializers.ModelSerializer):
    source_version = serializers.CharField(source="source.source_version", read_only=True)
    source = PreviewSourceSerializer(read_only=True)
    image_url = serializers.SerializerMethodField(help_text="Authenticated URL of this preview's own image (immutable).")
    latest_revision = serializers.SerializerMethodField()
    is_latest = serializers.SerializerMethodField(help_text="False when a newer revision of this image exists.")
    render = serializers.SerializerMethodField(help_text="What the renderer recorded: outcome, guard reductions, fallbacks, auto recipe.")

    class Meta:
        model = EditPreview
        fields = [
            "id", "enhancement_result", "revision", "source_version", "source", "mode", "requested_adjustments",
            "applied_adjustments", "render", "output_format", "width", "height", "file_size", "image_url",
            "latest_revision", "is_latest", "created_at", "expires_at",
        ]
        read_only_fields = fields

    def _describe(self, preview):
        from .previews import describe

        return describe(preview)

    def get_image_url(self, preview) -> str:
        path = f"/api/enhancements/previews/{preview.pk}/image/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_latest_revision(self, preview) -> int:
        return self._describe(preview)["latest_revision"]

    def get_is_latest(self, preview) -> bool:
        return self._describe(preview)["is_latest"]

    def get_render(self, preview) -> dict:
        metadata = preview.render_metadata or {}
        keys = ("outcome", "renderer_version", "render_policy", "validated", "safety_strength", "safety_reasons",
                "guard_reductions", "fallback_reasons", "auto", "transparency", "model_controls")
        return {key: metadata[key] for key in keys if key in metadata}


class PreviewListSerializer(serializers.Serializer):
    source_version = serializers.CharField(help_text="The current version of the enhanced image.")
    latest_revision = serializers.IntegerField(allow_null=True)
    previews = EditPreviewSerializer(many=True)


# -- cloud adjustment advisor (Phase C) --------------------------------------------

class AdvisorConsentRequestSerializer(serializers.Serializer):
    granted = serializers.BooleanField()
    policy_version = serializers.CharField(
        max_length=40, help_text="The policy_version shown by GET advisor/configuration/; required to grant.",
    )


class AdvisorRecommendRequestSerializer(serializers.Serializer):
    intent = serializers.ChoiceField(choices=["auto", "preserve", "brighten"], default="auto")


class AdvisorRunRequestSerializer(AdvisorRecommendRequestSerializer):
    result_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=200,
        help_text="Enhanced results to advise on; one independent job per image.",
    )


class AdvisorRecommendationSerializer(serializers.ModelSerializer):
    usable = serializers.SerializerMethodField(help_text="True only while this recommendation can still be applied.")
    unusable_reason = serializers.SerializerMethodField(
        help_text="cancelled, superseded, source_changed, edited_since, expired, source_unavailable, "
                  "not_finished, nothing_to_apply, or empty when usable.",
    )
    usage = serializers.SerializerMethodField()
    guard = serializers.SerializerMethodField()

    class Meta:
        model = AdvisorRecommendation
        fields = [
            "id", "enhancement_result", "run", "intent", "status", "origin", "action", "adjustments", "rationale",
            "caution_flags", "fallback_reason", "failure_reason", "usable", "unusable_reason", "model",
            "policy_version", "renderer_version", "render_policy", "source_version", "attempts", "usage", "guard",
            "created_at", "finished_at", "expires_at",
        ]
        read_only_fields = fields

    def _state(self, recommendation):
        from .advisor_service import staleness

        cached = getattr(recommendation, "_advisor_state", None)
        if cached is None:
            if recommendation.status != "completed":
                reason = recommendation.status if recommendation.status in ("cancelled", "superseded") else "not_finished"
            else:
                reason = staleness(recommendation)
                if not reason and recommendation.action != "apply":
                    reason = "nothing_to_apply"
            cached = recommendation._advisor_state = reason
        return cached

    def get_usable(self, recommendation) -> bool:
        return not self._state(recommendation)

    def get_unusable_reason(self, recommendation) -> str:
        return self._state(recommendation)

    def get_usage(self, recommendation) -> dict:
        return {
            "input_tokens": recommendation.input_tokens, "output_tokens": recommendation.output_tokens,
            "latency_ms": recommendation.latency_ms,
        }

    def get_guard(self, recommendation) -> dict:
        guard = recommendation.guard or {}
        return {key: guard[key] for key in (
            "clamps_and_locks", "reductions", "intent_locks", "safety_strength", "guard_reductions", "safety_reasons",
        ) if key in guard}


class AdvisorRunSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()
    recommendations = AdvisorRecommendationSerializer(many=True, read_only=True)

    class Meta:
        model = AdvisorRun
        fields = ["id", "intent", "status", "counts", "recommendations", "cancelled_at", "created_at"]
        read_only_fields = fields

    def _rows(self, run):
        return list(run.recommendations.all())

    def get_status(self, run) -> str:
        rows = self._rows(run)
        if any(row.status in ("queued", "processing") for row in rows):
            return "running"
        return "cancelled" if run.cancelled_at else "completed"

    def get_counts(self, run) -> dict:
        rows = self._rows(run)
        counts = {"total": len(rows)}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        counts["from_advisor"] = sum(1 for row in rows if row.origin == "openai")
        counts["local_fallback"] = sum(1 for row in rows if row.origin == "local_fallback")
        return counts

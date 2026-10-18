from rest_framework import serializers

from uploads.models import UploadBatch


class AdminFailedBatchSerializer(serializers.ModelSerializer):
    """A failed/stuck batch with its owner, for administrator visibility."""

    owner_id = serializers.IntegerField(read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True)

    class Meta:
        model = UploadBatch
        fields = [
            "id", "owner_id", "owner_username", "upload_type", "display_name",
            "processing_status", "current_stage", "total_files", "accepted_files",
            "processed_files", "processing_failed_files", "processing_error",
            "retry_count", "processing_started_at", "processing_finished_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


# -- Admin analytics (GET /api/system/analytics/) -----------------------------
# The response is rendered through these serializers, and a test compares them
# with operations.analytics.build_admin_analytics key by key.

class AnalyticsRangeSerializer(serializers.Serializer):
    """The time window every windowed total and chart covers."""

    # "from" is a Python keyword, so the fields are declared here.
    def get_fields(self):
        return {
            "from": serializers.DateTimeField(help_text="Window start (inclusive)."),
            "to": serializers.DateTimeField(help_text="Window end (inclusive)."),
        }


class AnalyticsFiltersSerializer(serializers.Serializer):
    status = serializers.CharField(allow_null=True)
    model = serializers.CharField(allow_null=True)
    owner = serializers.IntegerField(allow_null=True)


class AnalyticsUserTotalsSerializer(serializers.Serializer):
    total = serializers.IntegerField(help_text="All accounts, not limited to the window.")
    verified = serializers.IntegerField()
    suspended = serializers.IntegerField()


class AnalyticsImageTotalsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    accepted = serializers.IntegerField(help_text="Uploads that passed upload validation (status 'uploaded').")
    rejected = serializers.IntegerField(help_text="Uploads refused at upload validation.")


class AnalyticsBreakdownSerializer(serializers.Serializer):
    total = serializers.IntegerField()


class AnalyticsEnhancementTotalsSerializer(AnalyticsBreakdownSerializer):
    by_status = serializers.DictField(child=serializers.IntegerField())


class AnalyticsQualityTotalsSerializer(AnalyticsBreakdownSerializer):
    by_decision = serializers.DictField(child=serializers.IntegerField())


class AnalyticsTotalsSerializer(serializers.Serializer):
    users = AnalyticsUserTotalsSerializer()
    batches = serializers.IntegerField()
    images = AnalyticsImageTotalsSerializer()
    enhancements = AnalyticsEnhancementTotalsSerializer()
    quality_decisions = AnalyticsQualityTotalsSerializer()


class AnalyticsTimingSerializer(serializers.Serializer):
    enhancement = serializers.FloatField(allow_null=True)
    quality_analysis = serializers.FloatField(allow_null=True)


class AnalyticsCountSerializer(serializers.Serializer):
    value = serializers.CharField()
    count = serializers.IntegerField()


class AnalyticsStorageSerializer(serializers.Serializer):
    originals = serializers.IntegerField()
    enhanced_outputs = serializers.IntegerField()
    total = serializers.IntegerField()


class AnalyticsDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    count = serializers.IntegerField()


class AnalyticsChartsSerializer(serializers.Serializer):
    uploads_per_day = AnalyticsDaySerializer(many=True)
    enhancements_per_day = AnalyticsDaySerializer(many=True)


class OverviewRecentSerializer(serializers.Serializer):
    today = serializers.IntegerField(help_text="Photos received in the last 24 hours.")
    this_week = serializers.IntegerField(help_text="Photos received in the last 7 days.")
    this_month = serializers.IntegerField(help_text="Photos received in the last 30 days.")


class OverviewUploadTypeSerializer(serializers.Serializer):
    single = serializers.IntegerField()
    multiple = serializers.IntegerField()
    zip = serializers.IntegerField()


class OverviewUploadDaySerializer(OverviewUploadTypeSerializer):
    date = serializers.DateField()


class OverviewUploadsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    recent = OverviewRecentSerializer(help_text="Fixed windows ending now, whatever date range was asked for.")
    by_type = OverviewUploadTypeSerializer(help_text="Photos in the window by how they arrived.")
    average_file_size_bytes = serializers.IntegerField(allow_null=True)
    per_day_by_type = OverviewUploadDaySerializer(many=True)


class OverviewRejectionsSerializer(serializers.Serializer):
    total = serializers.IntegerField(help_text="Refused at upload or rejected by the quality decision, each photo once.")
    rate_percent = serializers.FloatField(allow_null=True, help_text="Share of the photos received in the window; null when none.")
    per_day = AnalyticsDaySerializer(many=True)


class OverviewEnhancementWeekSerializer(serializers.Serializer):
    week = serializers.CharField(help_text="ISO week, for example 2026-W42.")
    completed = serializers.IntegerField()
    failed = serializers.IntegerField()
    success_percent = serializers.FloatField(allow_null=True)
    failed_percent = serializers.FloatField(allow_null=True)


class OverviewLiveSessionsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    per_day = AnalyticsDaySerializer(many=True)


class OverviewStorageMonthSerializer(serializers.Serializer):
    month = serializers.CharField(help_text="YYYY-MM.")
    added_bytes = serializers.IntegerField()
    total_bytes = serializers.IntegerField(help_text="Cumulative at the end of that month.")


class OverviewStorageSerializer(serializers.Serializer):
    originals_and_outputs_bytes = serializers.IntegerField()
    zip_packages_bytes = serializers.IntegerField()
    camera_snapshots_bytes = serializers.IntegerField(help_text="Already counted inside originals_and_outputs_bytes.")
    capacity_bytes = serializers.IntegerField(allow_null=True, help_text="Size of the disk (filesystem storage); null on object storage.")
    used_percent = serializers.FloatField(allow_null=True)
    growth_by_month = OverviewStorageMonthSerializer(many=True)


class OverviewChangeSerializer(serializers.Serializer):
    uploads_percent = serializers.FloatField(allow_null=True)
    enhancements_percent = serializers.FloatField(allow_null=True)
    rejections_percent = serializers.FloatField(allow_null=True)
    live_sessions_percent = serializers.FloatField(allow_null=True)


class AnalyticsOverviewSerializer(serializers.Serializer):
    """What the administrator's Overview, Statistics, Upload Statistics and Rejections screens show beyond the core totals."""

    uploads = OverviewUploadsSerializer()
    rejections = OverviewRejectionsSerializer()
    enhancement_success_per_week = OverviewEnhancementWeekSerializer(many=True)
    live_sessions = OverviewLiveSessionsSerializer()
    storage = OverviewStorageSerializer()
    change_vs_previous_period = OverviewChangeSerializer(
        help_text="Percent change against the equally long window just before; null when that window is empty.",
    )


class AdminAnalyticsSerializer(serializers.Serializer):
    range = AnalyticsRangeSerializer()
    filters_applied = AnalyticsFiltersSerializer()
    totals = AnalyticsTotalsSerializer()
    success_rate_percent = serializers.FloatField(
        allow_null=True, help_text="Accepted quality decisions as a share of all decided ones; null when none.",
    )
    average_processing_time_ms = AnalyticsTimingSerializer()
    top_rejection_reasons = AnalyticsCountSerializer(many=True, help_text="Each reason occurrence counts once.")
    model_usage = AnalyticsCountSerializer(
        many=True, help_text="Each '+'-joined component of an algorithm string counts once per result.",
    )
    storage_bytes = AnalyticsStorageSerializer()
    charts = AnalyticsChartsSerializer()
    overview = AnalyticsOverviewSerializer()
    generated_at = serializers.DateTimeField()

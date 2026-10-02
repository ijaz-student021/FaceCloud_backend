from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers

from organizations.serializers import MemberUserSerializer

from .models import ModeratorReport, ModeratorReview

User = get_user_model()


class ModeratorReviewSerializer(serializers.ModelSerializer):
    assigned_moderator = MemberUserSerializer(read_only=True)
    decided_by = MemberUserSerializer(read_only=True)
    claimed_by = MemberUserSerializer(read_only=True)
    image_id = serializers.IntegerField(read_only=True)
    # Derived rather than stored, so a claim that has simply run out reads as
    # free in the queue without anything having to write to the row first.
    is_claimed = serializers.SerializerMethodField()
    claim_expires_at = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = ModeratorReview
        fields = [
            "id", "organization", "image_id", "source", "status",
            "ai_recommendation", "ai_confidence", "assigned_moderator",
            "claimed_by", "is_claimed", "claim_expires_at",
            "due_at", "is_overdue", "sla_breached_at",
            "decision_reason", "decision_notes", "decided_by", "decided_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_is_claimed(self, review) -> bool:
        return review.is_claimed()

    def get_claim_expires_at(self, review) -> str | None:
        expires_at = review.active_claim_expiry()
        return expires_at.isoformat() if expires_at else None

    def get_is_overdue(self, review) -> bool:
        return review.is_overdue()


class ModeratorDecisionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject", "correct", "keep_in_review"])
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    group_id = serializers.IntegerField(
        required=False, min_value=1,
        help_text="Identity reviews only: the person folder to assign on a 'correct' decision. "
        "Omit to start a new folder instead.",
    )


class ModeratorEscalateSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ModeratorAssignSerializer(serializers.Serializer):
    moderator_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), source="moderator")


class ModeratorReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModeratorReport
        fields = [
            "id", "organization", "requested_by", "report_type", "format", "status",
            "file_size", "failure_reason", "generated_at", "expires_at", "created_at",
        ]
        read_only_fields = fields


class ModeratorReportCreateSerializer(serializers.Serializer):
    report_type = serializers.ChoiceField(choices=ModeratorReport.ReportType.choices)
    format = serializers.ChoiceField(choices=ModeratorReport.Format.choices, default=ModeratorReport.Format.JSON)


class ModeratorDuplicateGroupMergeSerializer(serializers.Serializer):
    source_group_id = serializers.IntegerField(required=False, min_value=1)
    target_group_id = serializers.IntegerField(required=False, min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ModeratorDashboardSerializer(serializers.Serializer):
    pending = serializers.IntegerField()
    in_review = serializers.IntegerField()
    escalated = serializers.IntegerField()
    completed_today = serializers.IntegerField()
    approved_total = serializers.IntegerField()
    rejected_total = serializers.IntegerField()
    corrected_total = serializers.IntegerField()
    overdue = serializers.IntegerField()
    due_soon = serializers.IntegerField()
    unassigned = serializers.IntegerField()
    claimed_now = serializers.IntegerField()
    oldest_open_age_hours = serializers.FloatField(allow_null=True)
    sla_hours = serializers.IntegerField()


class ModeratorBulkDecisionSerializer(serializers.Serializer):
    review_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False,
        max_length=settings.MODERATION_BULK_MAX_REVIEWS,
        help_text="Reviews to decide. Duplicates are collapsed.",
    )
    action = serializers.ChoiceField(choices=["approve", "reject", "escalate"])
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ModeratorBulkFailureSerializer(serializers.Serializer):
    review_id = serializers.IntegerField()
    detail = serializers.CharField()


class ModeratorBulkResultSerializer(serializers.Serializer):
    """Per-review outcomes: one unusable row never fails the whole selection."""

    action = serializers.CharField()
    requested = serializers.IntegerField()
    succeeded = serializers.ListField(child=serializers.IntegerField())
    succeeded_count = serializers.IntegerField()
    failed = ModeratorBulkFailureSerializer(many=True)
    failed_count = serializers.IntegerField()


class ModeratorPerformanceRowSerializer(serializers.Serializer):
    moderator_id = serializers.IntegerField()
    username = serializers.CharField()
    decided_count = serializers.IntegerField()
    approved = serializers.IntegerField()
    rejected = serializers.IntegerField()
    corrected = serializers.IntegerField()
    on_time = serializers.IntegerField()
    late = serializers.IntegerField()
    on_time_rate = serializers.FloatField(allow_null=True)
    average_decision_hours = serializers.FloatField(allow_null=True)
    slowest_decision_hours = serializers.FloatField(allow_null=True)


class ModeratorPerformanceTotalsSerializer(serializers.Serializer):
    decided_count = serializers.IntegerField()
    on_time = serializers.IntegerField()
    late = serializers.IntegerField()
    on_time_rate = serializers.FloatField(allow_null=True)
    average_decision_hours = serializers.FloatField(allow_null=True)
    open_now = serializers.IntegerField()
    overdue_now = serializers.IntegerField()


class ModeratorPerformanceSerializer(serializers.Serializer):
    window_days = serializers.IntegerField()
    since = serializers.DateTimeField()
    sla_hours = serializers.IntegerField()
    moderators = ModeratorPerformanceRowSerializer(many=True)
    totals = ModeratorPerformanceTotalsSerializer()

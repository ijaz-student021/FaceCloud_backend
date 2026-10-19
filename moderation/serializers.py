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
    # The photo's own name, so a row in the queue is recognisable without
    # fetching the image first. The thumbnail itself is a separate request
    # (ModeratorReviewImageView) because it is binary.
    image_name = serializers.CharField(source="image.original_name", read_only=True)
    # Derived rather than stored, so a claim that has simply run out reads as
    # free in the queue without anything having to write to the row first.
    is_claimed = serializers.SerializerMethodField()
    claim_expires_at = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = ModeratorReview
        fields = [
            "id", "organization", "image_id", "image_name", "source", "status",
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



class ModeratorReviewDetailSerializer(ModeratorReviewSerializer):
    """The review as the Image Review screen opens it: the queue row plus the photograph's own facts, so the screen needs no second request."""

    image = serializers.SerializerMethodField(help_text=(
        "The photograph under review: id, name, width, height, file_size, uploader, face_count, quality (decision, overall_score, "
        "rejection_reasons, warnings) and preview_url (the review-keyed thumbnail)."
    ))

    class Meta(ModeratorReviewSerializer.Meta):
        fields = ModeratorReviewSerializer.Meta.fields + ["image"]
        read_only_fields = fields

    def get_image(self, review) -> dict:
        from django.urls import reverse

        image = review.image
        quality = getattr(image, "quality_decision", None)
        validation = getattr(image, "face_validation", None)
        request = self.context.get("request")
        path = reverse("moderation:review-image", kwargs={"organization_id": review.organization_id, "review_id": review.pk})
        return {
            "id": image.pk, "name": image.original_name, "width": image.width, "height": image.height, "file_size": image.file_size,
            "uploader": {"id": image.owner_id, "username": image.owner.username},
            "face_count": validation.face_count if validation else None,
            "quality": {
                "decision": (quality.decision or None) if quality else None,
                "overall_score": quality.overall_score if quality else None,
                "rejection_reasons": list(quality.rejection_reasons or []) if quality else [],
                "warnings": list(getattr(quality, "warnings", None) or []) if quality else [],
            },
            "preview_url": request.build_absolute_uri(path) if request is not None else path,
        }

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
    requested_by_name = serializers.SerializerMethodField(help_text="The username of whoever asked for it; null when that account is gone.")
    download_url = serializers.SerializerMethodField(help_text="Where to fetch the file (with the Authorization header); null until the report is ready.")

    class Meta:
        model = ModeratorReport
        fields = [
            "id", "organization", "requested_by", "requested_by_name", "report_type", "format", "status",
            "date_from", "date_to", "download_url",
            "file_size", "failure_reason", "generated_at", "expires_at", "created_at",
        ]
        read_only_fields = fields

    def get_requested_by_name(self, report) -> str | None:
        return report.requested_by.username if report.requested_by else None

    def get_download_url(self, report) -> str | None:
        from django.urls import reverse

        if report.status != ModeratorReport.Status.READY:
            return None
        path = reverse("moderation:report-download", kwargs={"organization_id": report.organization_id, "report_id": report.pk})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request is not None else path


MAX_REPORT_PERIOD_DAYS = 366


class ModeratorReportCreateSerializer(serializers.Serializer):
    """``date_from``/``date_to`` are optional (UTC). A plain date for date_to
    includes that whole day; the window is half-open [date_from, date_to)."""

    report_type = serializers.ChoiceField(choices=ModeratorReport.ReportType.choices)
    format = serializers.ChoiceField(choices=ModeratorReport.Format.choices, default=ModeratorReport.Format.JSON)
    date_from = serializers.CharField(required=False, help_text="ISO date or datetime, UTC; inclusive.")
    date_to = serializers.CharField(required=False, help_text="ISO date (whole day included) or datetime (exclusive), UTC.")

    def _parse(self, value, field, end):
        from datetime import datetime, time, timedelta, timezone as dt_timezone

        from django.utils.dateparse import parse_date, parse_datetime

        value = value.strip()
        # A plain date first: parse_datetime also accepts "2026-09-30" (as
        # midnight), which would drop the last day of the range.
        day = parse_date(value) if len(value) == 10 else None
        if day is not None:
            return datetime.combine(day + timedelta(days=1) if end else day, time.min, tzinfo=dt_timezone.utc)
        moment = parse_datetime(value)
        if moment is None:
            raise serializers.ValidationError({field: "Use an ISO date (2026-10-01) or datetime."})
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt_timezone.utc)
        return moment.astimezone(dt_timezone.utc)

    def validate(self, attrs):
        from datetime import timedelta

        start = self._parse(attrs["date_from"], "date_from", end=False) if attrs.get("date_from") else None
        end = self._parse(attrs["date_to"], "date_to", end=True) if attrs.get("date_to") else None
        if start and end:
            if start >= end:
                raise serializers.ValidationError({"date_to": "date_to must be after date_from."})
            if end - start > timedelta(days=MAX_REPORT_PERIOD_DAYS):
                raise serializers.ValidationError({"date_to": f"A report covers at most {MAX_REPORT_PERIOD_DAYS} days."})
        attrs["date_from"], attrs["date_to"] = start, end
        return attrs


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


class ModeratorImageSerializer(serializers.Serializer):
    """One photograph submitted to the organization, as the Rejected Images screen draws it."""

    id = serializers.IntegerField()
    original_name = serializers.CharField()
    batch_id = serializers.IntegerField()
    uploader = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)
    file_size = serializers.IntegerField()
    decision = serializers.SerializerMethodField(help_text="rejected (refused at upload or by the quality decision), needs_review, accepted, or null while undecided.")
    rejection_reasons = serializers.SerializerMethodField(help_text="Reason codes from the quality decision; for a file refused at upload, its one-sentence reason.")
    overall_score = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField(help_text="Thumbnail (the moderator image preview); null for a file refused at upload.")
    review_id = serializers.SerializerMethodField(help_text="The newest review of this photo in this organization, when there is one.")

    def get_uploader(self, image) -> dict:
        return {"id": image.owner_id, "username": image.owner.username}

    def _quality(self, image):
        return getattr(image, "quality_decision", None)

    def get_decision(self, image) -> str | None:
        if image.status == "rejected":
            return "rejected"
        quality = self._quality(image)
        return (quality.decision or None) if quality else None

    def get_rejection_reasons(self, image) -> list:
        if image.status == "rejected":
            return [image.rejection_reason] if image.rejection_reason else []
        quality = self._quality(image)
        return list(quality.rejection_reasons or []) if quality else []

    def get_overall_score(self, image) -> float | None:
        quality = self._quality(image)
        return quality.overall_score if quality else None

    def get_preview_url(self, image) -> str | None:
        from django.urls import reverse

        if image.status == "rejected":
            return None
        path = reverse("moderation:image-preview", kwargs={"organization_id": self.context["organization"].pk, "image_id": image.pk})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request is not None else path

    def get_review_id(self, image) -> int | None:
        reviews = getattr(image, "org_reviews", None)
        return reviews[0].pk if reviews else None

from django.contrib.auth import get_user_model
from rest_framework import serializers

from organizations.serializers import MemberUserSerializer

from .models import ModeratorReview

User = get_user_model()


class ModeratorReviewSerializer(serializers.ModelSerializer):
    assigned_moderator = MemberUserSerializer(read_only=True)
    decided_by = MemberUserSerializer(read_only=True)
    image_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = ModeratorReview
        fields = [
            "id", "organization", "image_id", "source", "status",
            "ai_recommendation", "ai_confidence", "assigned_moderator",
            "decision_reason", "decision_notes", "decided_by", "decided_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


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


class ModeratorDashboardSerializer(serializers.Serializer):
    pending = serializers.IntegerField()
    in_review = serializers.IntegerField()
    escalated = serializers.IntegerField()
    completed_today = serializers.IntegerField()
    approved_total = serializers.IntegerField()
    rejected_total = serializers.IntegerField()
    corrected_total = serializers.IntegerField()

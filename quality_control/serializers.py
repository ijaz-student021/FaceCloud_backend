from rest_framework import serializers

from .models import ImageQualityDecision


class ImageQualityDecisionSerializer(serializers.ModelSerializer):
    effective_decision = serializers.SerializerMethodField(help_text=(
        "What the verdict means for the person: 'needs_review' only where a moderator can decide it (a photograph filed under an organization); in a personal workspace "
        "nobody can review, so a stored 'needs_review' is 'accepted' here. 'decision' is what the checks found and never changes."))
    review_required = serializers.SerializerMethodField(help_text="True only when a moderator has to decide this photograph (needs_review, in an organization).")
    tips = serializers.SerializerMethodField(help_text="Plain sentences about what the checks noticed (a little dim, no face found), for a photograph that was accepted anyway.")

    def get_effective_decision(self, obj) -> str:
        from .effective import effective_decision

        return effective_decision(obj.image, obj.decision)

    def get_review_required(self, obj) -> bool:
        from .effective import review_required

        return review_required(obj.image, obj.decision)

    def get_tips(self, obj) -> list[str]:
        from .effective import face_tips, image_reason_codes, tips_for

        if obj.decision not in ("accepted", "needs_review"):
            return []
        return tips_for(image_reason_codes(obj.image)) + face_tips(obj.image)

    class Meta:
        model = ImageQualityDecision
        fields = [
            "id", "image", "face_validation", "status", "decision", "effective_decision", "review_required", "tips", "overall_score",
            "brightness_score", "sharpness_score", "contrast_score", "noise_score",
            "exposure_score", "resolution_score", "measurements", "defects",
            "rejection_reasons", "message", "policy_version", "task_id",
            "failure_reason", "processing_duration_ms", "started_at", "finished_at",
            "created_at", "updated_at",
        ]

from rest_framework import serializers

from .models import DuplicateAnalysis, DuplicateMatch


class DuplicateAnalyzeRequestSerializer(serializers.Serializer):
    force = serializers.BooleanField(required=False, default=False)


class DuplicateAnalysisSerializer(serializers.ModelSerializer):
    matches_url = serializers.SerializerMethodField()

    class Meta:
        model = DuplicateAnalysis
        fields = [
            "id", "image", "status", "decision", "match_count",
            "fingerprint_provider", "policy_version", "failure_reason",
            "processing_duration_ms", "matches_url", "started_at", "finished_at",
            "created_at", "updated_at",
        ]

    def get_matches_url(self, obj) -> str:
        path = f"/api/duplicates/images/{obj.image_id}/matches/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class DuplicateMatchSerializer(serializers.ModelSerializer):
    source_filename = serializers.CharField(source="source_image.original_name", read_only=True)
    matched_filename = serializers.CharField(source="matched_image.original_name", read_only=True)
    source_image_download_url = serializers.SerializerMethodField()
    matched_image_download_url = serializers.SerializerMethodField()

    class Meta:
        model = DuplicateMatch
        fields = [
            "id", "analysis", "source_image", "source_filename", "matched_image",
            "matched_filename", "source_image_download_url",
            "matched_image_download_url", "match_type",
            "similarity_score", "hash_distance", "face_similarity", "evidence",
            "review_status", "reviewed_at", "created_at", "updated_at",
        ]

    def get_matched_image_download_url(self, obj) -> str:
        path = f"/api/images/{obj.matched_image_id}/download/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    def get_source_image_download_url(self, obj) -> str:
        path = f"/api/images/{obj.source_image_id}/download/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class DuplicateMatchReviewSerializer(serializers.Serializer):
    review_status = serializers.ChoiceField(
        choices=[
            DuplicateMatch.ReviewStatus.CONFIRMED,
            DuplicateMatch.ReviewStatus.NOT_DUPLICATE,
        ]
    )

from rest_framework import serializers

from uploads.models import ImageUpload
from uploads.serializers import MyImageSerializer


class IdentityGroupImageSerializer(MyImageSerializer):
    """One photo in a folder: the images list's own status plus how well it matched the folder."""

    matching_confidence = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = (
            "id", "original_name", "thumbnail_url", "file_size", "width", "height",
            "display_status", "matching_confidence",
        )

    def get_matching_confidence(self, obj) -> float | None:
        """The photo's best score against this folder as a percentage; null when it has none."""
        score = getattr(obj, "group_similarity", None)
        return round(100 * score, 1) if score is not None else None

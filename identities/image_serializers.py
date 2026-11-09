from rest_framework import serializers

from uploads.models import ImageUpload
from uploads.serializers import MyImageSerializer


class IdentityGroupImageSerializer(MyImageSerializer):
    """One photo in a folder: the images list's own status plus how well it matched the folder."""

    matching_confidence = serializers.SerializerMethodField()
    membership_id = serializers.SerializerMethodField()
    membership_state = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = (
            "id", "original_name", "thumbnail_url", "file_size", "width", "height",
            "display_status", "matching_confidence", "membership_id", "membership_state",
        )

    def get_membership_id(self, obj) -> int | None:
        """The membership that puts this photo in this folder (the best-scoring one when several faces of the photo are here).

        It is what move, remove and cover-photo actions take: ``PATCH /api/identities/memberships/{membership_id}/move/`` and ``.../remove/``,
        and ``{"membership_id": ...}`` on ``PATCH /api/identities/groups/{id}/profile/``."""
        return getattr(obj, "group_membership_id", None)

    def get_membership_state(self, obj) -> str | None:
        """``confirmed``, ``needs_review`` and so on: the state of that membership."""
        return getattr(obj, "group_membership_state", None)

    def get_matching_confidence(self, obj) -> float | None:
        """The photo's best score against this folder as a percentage; null when it has none."""
        score = getattr(obj, "group_similarity", None)
        return round(100 * score, 1) if score is not None else None

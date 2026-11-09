"""Which photographs may be enhanced.

A photograph that the checks rejected is not enhanced, whoever asks and however: a rejected photograph never gets an enhanced copy, so it can
never end up in a package, an export or a share link as one. The checks, in the order they speak:

| Check | Value | May be enhanced? |
|---|---|---|
| upload (``ImageUpload.status``) | ``rejected`` | no |
| quality decision | ``rejected`` | no, only while QUALITY_CHECKS_REJECT is on (it is off: a quality check never refuses a photograph) |
| face validation | ``rejected`` | no |
| quality decision / face validation | ``needs_review`` | yes: a person asked for it, and a moderator may still decide |
| no decision recorded | | yes |

Used by the single-photograph request and by the whole-batch request, which skips such photographs instead of failing.
"""
from django.conf import settings
from django.db.models import Q

REJECTED = "rejected"
REFUSAL = "This photo was rejected by the checks, so it is not enhanced."


def not_rejected(queryset):
    """``queryset`` of ImageUpload without the photographs that were rejected at upload, by quality or by face validation."""
    refused = Q(status=REJECTED) | Q(face_validation__decision=REJECTED)
    if settings.QUALITY_CHECKS_REJECT:
        refused |= Q(quality_decision__decision=REJECTED)
    return queryset.exclude(refused)


def rejection_of(image):
    """``None`` when ``image`` may be enhanced, otherwise the sentence to show."""
    return None if not_rejected(type(image).objects.filter(pk=image.pk)).exists() else REFUSAL

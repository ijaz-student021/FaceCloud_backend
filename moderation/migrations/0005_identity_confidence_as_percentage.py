from django.db import migrations
from django.db.models import F


def to_percentage(apps, schema_editor):
    """Bring identity reviews onto the same 0-100 scale as the other two sources.

    Only identity rows are touched: quality scores and duplicate similarities
    were already percentages. Identity similarities are fractions by
    construction (IdentityMembership stores matching_distance as 1 - score), so
    every existing identity row needs the same conversion.
    """
    ModeratorReview = apps.get_model("moderation", "ModeratorReview")
    ModeratorReview.objects.filter(
        source="identity", ai_confidence__isnull=False,
    ).update(ai_confidence=F("ai_confidence") * 100)


def to_fraction(apps, schema_editor):
    ModeratorReview = apps.get_model("moderation", "ModeratorReview")
    ModeratorReview.objects.filter(
        source="identity", ai_confidence__isnull=False,
    ).update(ai_confidence=F("ai_confidence") / 100)


class Migration(migrations.Migration):

    dependencies = [
        ("moderation", "0004_moderatorreview_claimed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(to_percentage, to_fraction),
    ]

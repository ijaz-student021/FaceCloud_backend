from django.db import migrations, models

CHOICES = [
    ("own_members", "Each moderator sees their own members"),
    ("organization", "Every moderator sees the whole organization"),
]


class Migration(migrations.Migration):
    """Add Organization.moderator_data_scope.

    Two steps on purpose: every organization that exists now is created with ``organization`` (today's behaviour, nothing changes for it), and only then does
    the column's default become ``own_members``, which is what organizations created from now on start with.
    """

    dependencies = [
        ("organizations", "0009_moderator_preview_and_reprocess_actions"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="moderator_data_scope",
            field=models.CharField(choices=CHOICES, default="organization", max_length=16),
        ),
        migrations.AlterField(
            model_name="organization",
            name="moderator_data_scope",
            field=models.CharField(choices=CHOICES, default="own_members", max_length=16),
        ),
    ]

from django.db import migrations

# Rows written before the frontend's routes were checked pointed at pages that do not exist.
RENAMED = {
    "/dashboard/uploads": "/dashboard/jobs",
    "/dashboard/person-folders": "/dashboard/groups",
}


def fix_paths(apps, schema_editor):
    UserNotification = apps.get_model("notifications", "UserNotification")
    for old, new in RENAMED.items():
        UserNotification.objects.filter(action_path=old).update(action_path=new)


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0004_alter_usernotification_event_type"),
    ]

    operations = [
        # Not reversible on purpose: the old paths were wrong.
        migrations.RunPython(fix_paths, migrations.RunPython.noop),
    ]

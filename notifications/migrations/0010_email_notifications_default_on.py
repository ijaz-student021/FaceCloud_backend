from datetime import timedelta

from django.db import migrations, models
from django.db.models import F


def turn_on_for_untouched(apps, schema_editor):
    """Accounts that never changed their preferences (the row was last saved when it was created) get the new default; a choice somebody made stays."""
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")
    NotificationPreference.objects.filter(email_notifications=False, updated_at__lte=F("created_at") + timedelta(seconds=2)).update(email_notifications=True)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0009_alter_usernotification_event_type")]

    operations = [
        migrations.AlterField(model_name="notificationpreference", name="email_notifications", field=models.BooleanField(default=True)),
        migrations.RunPython(turn_on_for_untouched, migrations.RunPython.noop),
    ]

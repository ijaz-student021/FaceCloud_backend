from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("quality_control", "0001_initial")]

    operations = [
        migrations.AddField(model_name="imagequalitydecision", name="decided_by_person", field=models.BooleanField(default=False)),
    ]

"""Photo collages (Feature 49) were removed from the product: drop their table.

The ``collages`` app no longer exists, so its own migrations cannot run; this migration lives here, depends on nothing from it, and nothing depends on the
table (the only foreign key goes out, to the user). ``IF EXISTS`` makes it a no-op on a database that never had the table (a new installation, where
``collages.0001_initial`` is not in the graph any more) and safe to run twice.

Not touched, on purpose:

* the ``collages.0001_initial`` row in ``django_migrations`` (Django ignores rows of apps it does not know; deleting it adds nothing);
* the ``django_content_type`` / ``auth_permission`` rows of the removed model, which are harmless; ``manage.py remove_stale_contenttypes`` clears them;
* the generated files under ``users/<id>/collages/``: ``privacy.cleanup.RETIRED_MEDIA_SUBDIRECTORIES`` lets the orphan sweep remove them.

It cannot be reversed: the collages and their files are gone for good, so going backwards only does nothing.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("privacy", "0004_organizationexport"),
    ]

    operations = [
        migrations.RunSQL(
            sql='DROP TABLE IF EXISTS "collages_collage"',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

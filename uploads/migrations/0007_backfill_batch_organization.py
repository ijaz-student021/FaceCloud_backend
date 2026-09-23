"""Tag every pre-existing batch with its owner's personal organization.

Not reversible for the same reason organizations.0002 isn't: undoing it would
mean nulling out a field that real code (uploads.services.create_image_batch/
create_zip_batch) now populates on every new batch going forward.
"""
from django.db import migrations


def backfill_batch_organization(apps, schema_editor):
    UploadBatch = apps.get_model("uploads", "UploadBatch")
    OrganizationMembership = apps.get_model("organizations", "OrganizationMembership")

    personal_org_by_owner = dict(
        OrganizationMembership.objects.filter(
            role="owner", status="active", organization__is_personal=True,
        ).values_list("user_id", "organization_id")
    )
    for owner_id, organization_id in personal_org_by_owner.items():
        UploadBatch.objects.filter(owner_id=owner_id, organization__isnull=True).update(
            organization_id=organization_id,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("uploads", "0006_uploadbatch_organization"),
    ]

    operations = [
        migrations.RunPython(backfill_batch_organization, migrations.RunPython.noop),
    ]

"""Tag every pre-existing identity group with its owner's personal organization.

See uploads.migrations.0007 for the same pattern and why this isn't reversible.
"""
from django.db import migrations


def backfill_group_organization(apps, schema_editor):
    IdentityGroup = apps.get_model("identities", "IdentityGroup")
    OrganizationMembership = apps.get_model("organizations", "OrganizationMembership")

    personal_org_by_owner = dict(
        OrganizationMembership.objects.filter(
            role="owner", status="active", organization__is_personal=True,
        ).values_list("user_id", "organization_id")
    )
    for owner_id, organization_id in personal_org_by_owner.items():
        IdentityGroup.objects.filter(owner_id=owner_id, organization__isnull=True).update(
            organization_id=organization_id,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("identities", "0004_identitygroup_organization"),
    ]

    operations = [
        migrations.RunPython(backfill_group_organization, migrations.RunPython.noop),
    ]

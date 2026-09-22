"""Give every pre-existing user the personal organization new accounts get automatically.

Deliberately not reversible: undoing it would mean deleting organizations that
may, by the time this is ever rolled back, already have real members or
content attached to them (see organizations.services.create_personal_organization
and Phase B's per-app organization backfill, which depends on every owner
already having exactly one organization here).
"""
from django.conf import settings
from django.db import migrations
from django.utils.text import slugify


def _unique_slug(Organization, base):
    candidate = slugify(base)[:50] or "org"
    slug = candidate
    suffix = 1
    while Organization.objects.filter(slug=slug).exists():
        suffix += 1
        slug = f"{candidate}-{suffix}"
    return slug


def backfill_personal_organizations(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL)
    Organization = apps.get_model("organizations", "Organization")
    OrganizationMembership = apps.get_model("organizations", "OrganizationMembership")
    OrganizationAuditEvent = apps.get_model("organizations", "OrganizationAuditEvent")

    existing_user_ids = set(
        OrganizationMembership.objects.filter(role="owner").values_list("user_id", flat=True)
    )
    for user in User.objects.exclude(pk__in=existing_user_ids).iterator():
        organization = Organization.objects.create(
            name=f"{user.username}'s workspace",
            slug=_unique_slug(Organization, user.username),
            status="active", plan="free", is_personal=True,
        )
        OrganizationMembership.objects.create(
            organization=organization, user=user, role="owner", status="active",
        )
        OrganizationAuditEvent.objects.create(
            organization=organization, actor=user, action="organization_created",
            target_type="Organization", target_id=organization.pk,
            new_value={"name": organization.name, "owner": user.pk, "backfilled": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_personal_organizations, migrations.RunPython.noop),
    ]

"""Give every account that has no personal workspace one.

    python manage.py ensure_personal_workspaces            # dry run: only counts and lists
    python manage.py ensure_personal_workspaces --apply    # creates the missing personal workspaces

An account made by an administrator (before ``create_account_as_admin`` created the workspace) or from before workspaces existed has none. Nothing is moved: photographs
already filed stay where they are; the workspace only gives the account a private place for what it uploads from now on. Safe to run again.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from organizations.models import Organization, OrganizationMembership
from organizations.services import ensure_personal_organization


class Command(BaseCommand):
    help = "Create the personal workspace of every account that has none (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Create the workspaces. Without it nothing is changed.")

    def handle(self, *args, **options):
        has_one = OrganizationMembership.objects.filter(
            organization__is_personal=True, role=OrganizationMembership.Role.OWNER, status=OrganizationMembership.Status.ACTIVE,
        ).values("user_id")
        missing = get_user_model().objects.exclude(pk__in=has_one).order_by("pk")
        count = missing.count()
        if not options["apply"]:
            self.stdout.write(f"{count} account(s) have no personal workspace (dry run: nothing changed; use --apply).")
            for user in missing[:20]:
                self.stdout.write(f"  {user.pk}  {user.get_username()}")
            return
        created = 0
        for user in missing.iterator():
            before = Organization.objects.filter(is_personal=True).count()
            ensure_personal_organization(user)
            created += Organization.objects.filter(is_personal=True).count() - before
        self.stdout.write(f"Created {created} personal workspace(s) for {count} account(s).")

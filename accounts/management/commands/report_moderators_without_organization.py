"""List the moderator accounts that belong to no organization (read-only: nothing is changed or demoted)."""
from django.core.management.base import BaseCommand

from accounts.models import User
from accounts.services import has_moderator_organization


class Command(BaseCommand):
    help = (
        "Print the username and email of every account whose role is moderator but that has no active moderator or owner membership in an "
        "active organization. Such an account cannot sign in. Changes nothing: fix each by adding it to an organization or changing its role."
    )

    def handle(self, *args, **options):
        stranded = [user for user in User.objects.filter(role="moderator").order_by("username") if not has_moderator_organization(user)]
        for user in stranded:
            self.stdout.write(f"{user.username}\t{user.email}")
        self.stdout.write(self.style.WARNING(f"{len(stranded)} moderator account(s) without an organization.") if stranded
                          else self.style.SUCCESS("Every moderator account belongs to an organization."))

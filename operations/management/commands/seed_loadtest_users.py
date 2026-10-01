from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from organizations.services import create_personal_organization

User = get_user_model()

LOADTEST_PASSWORD = "LoadTest!Pass123"
USER_USERNAME_TEMPLATE = "loadtest-user-{n}"
ADMIN_USERNAME = "loadtest-admin"


class Command(BaseCommand):
    """Feature 45: seed deterministic, verified accounts for the Locust load test.

    Never usable against a real deployment: refuses unless ``settings.DEBUG`` is
    on, since every account gets the same public, well-known password. Idempotent
    (``get_or_create``), so re-running after wiping a local/CI database is safe.
    """

    help = "Seed verified test users (and one admin) with a known password, for load testing only."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=20, help="Number of regular users to seed (default 20).")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to seed load-test accounts: settings.DEBUG is off. "
                "This command creates accounts with a public, shared password and must "
                "never run against a real deployment."
            )

        count = options["count"]
        if count < 1:
            raise CommandError("--count must be at least 1.")

        created_users = 0
        for n in range(1, count + 1):
            username = USER_USERNAME_TEMPLATE.format(n=n)
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@loadtest.local",
                    "is_email_verified": True,
                },
            )
            if was_created:
                user.set_password(LOADTEST_PASSWORD)
                user.save(update_fields=["password"])
                create_personal_organization(user)
                created_users += 1

        admin, admin_created = User.objects.get_or_create(
            username=ADMIN_USERNAME,
            defaults={
                "email": f"{ADMIN_USERNAME}@loadtest.local",
                "is_email_verified": True,
                "role": "admin",
            },
        )
        if admin_created:
            admin.set_password(LOADTEST_PASSWORD)
            admin.save(update_fields=["password"])
            create_personal_organization(admin)
        elif admin.role != "admin":
            admin.role = "admin"
            admin.save(update_fields=["role"])

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {created_users} new user(s) (of {count} requested) and "
            f"{'1 new' if admin_created else '0 new (already existed)'} admin. "
            f"Shared password: {LOADTEST_PASSWORD}"
        ))

from django.core.management.base import BaseCommand

from accounts.services import purge_stale_registrations


class Command(BaseCommand):
    help = "Delete expired or completed signup registrations past retention (also runs daily from the Celery beat schedule)."

    def handle(self, *args, **options):
        deleted = purge_stale_registrations()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} pending registration records."))

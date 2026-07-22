from django.core.management.base import BaseCommand

from notifications.tasks import cleanup_expired_notifications


class Command(BaseCommand):
    help = "Delete read user notifications older than the configured retention period."

    def handle(self, *args, **options):
        deleted = cleanup_expired_notifications()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired notifications."))


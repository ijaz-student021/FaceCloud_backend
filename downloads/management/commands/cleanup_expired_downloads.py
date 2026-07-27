from django.core.management.base import BaseCommand

from downloads.services import expire_due_packages


class Command(BaseCommand):
    help = "Remove expired generated download-package files."

    def handle(self, *args, **options):
        expired = expire_due_packages()
        self.stdout.write(self.style.SUCCESS(f"Expired download packages cleaned: {expired}"))

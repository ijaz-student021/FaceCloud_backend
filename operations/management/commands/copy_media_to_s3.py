import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from operations.media_to_s3 import copy_media, default_root, new_storage


class Command(BaseCommand):
    help = (
        "Copy the files in the local media folder into the private S3 bucket (STORAGE_BACKEND=s3 settings). Local files "
        "are never changed or deleted; each copy is read back and checked against the original."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", default=None, help="Local media folder (default: MEDIA_ROOT).")
        parser.add_argument("--dry-run", action="store_true", help="Count what would be copied; copy nothing.")
        parser.add_argument("--limit", type=int, default=None, help="Stop after this many files.")

    def handle(self, *args, **options):
        if settings.STORAGE_BACKEND != "s3":
            raise CommandError("Set STORAGE_BACKEND=s3 and the bucket settings first; the bucket is the destination.")
        summary = copy_media(new_storage(), options["source"] or default_root(), options["dry_run"], options["limit"])
        self.stdout.write(json.dumps(summary, indent=2))
        if summary["failed"]:
            raise SystemExit(1)

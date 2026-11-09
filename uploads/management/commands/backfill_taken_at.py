"""Fill ``ImageUpload.taken_at`` for photographs uploaded before the capture time was read (``uploads/capture.py``).

    python manage.py backfill_taken_at [--limit N] [--dry-run]

Reads each stored original that has no ``taken_at`` yet and sets it when the file carries an EXIF capture time. Photographs without one stay empty (they are dated by their upload time).
Safe to run again: it only looks at photographs that are still empty, and it never touches anything else.
"""
from django.core.management.base import BaseCommand
from PIL import Image

from uploads.capture import capture_time
from uploads.models import ImageUpload


class Command(BaseCommand):
    help = "Read the EXIF capture time of stored photographs that have none yet."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many photographs (0: all).")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be set; change nothing.")

    def handle(self, *args, **options):
        queryset = ImageUpload.objects.filter(status="uploaded", taken_at__isnull=True).exclude(original_file="").order_by("pk")
        if options["limit"]:
            queryset = queryset[: options["limit"]]
        looked = found = unreadable = 0
        for image in queryset.iterator():
            looked += 1
            try:
                image.original_file.open("rb")
                try:
                    with Image.open(image.original_file) as source:
                        moment = capture_time(source)
                finally:
                    image.original_file.close()
            except Exception:
                unreadable += 1
                continue
            if moment is None:
                continue
            found += 1
            if not options["dry_run"]:
                ImageUpload.objects.filter(pk=image.pk).update(taken_at=moment)
        verb = "would set" if options["dry_run"] else "set"
        self.stdout.write(f"Looked at {looked} photograph(s): {verb} the capture time of {found}, {unreadable} could not be read, {looked - found - unreadable} have no EXIF time.")

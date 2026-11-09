"""Read the GPS position of photographs uploaded before places existed, and place the ones that have one.

    python manage.py backfill_location [--limit N] [--dry-run]

Two passes, both safe to repeat:

1. photographs whose file has never been looked at (``location_checked_at`` empty): the position is read from the stored original and the photograph is marked as looked at, with or
   without one. A photograph whose person removed its location is marked too, so it is never read again;
2. photographs that have a position but no place: placed from the offline gazetteer, when one is installed and approved (``places/gazetteer.py``); otherwise skipped and reported.

Nothing else about a photograph is changed.
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from PIL import Image

from places import gazetteer
from places.service import attach_place
from uploads.capture import gps_position
from uploads.models import ImageUpload


class Command(BaseCommand):
    help = "Read the GPS position of stored photographs not yet looked at, and place those that have one."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="Stop each pass after this many photographs (0: all).")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be done; change nothing.")

    def handle(self, *args, **options):
        if not settings.LOCATION_ENABLED:
            self.stdout.write("LOCATION_ENABLED is off: nothing to do.")
            return
        limit, dry = options["limit"], options["dry_run"]
        unseen = ImageUpload.all_objects.filter(status="uploaded", location_checked_at__isnull=True).exclude(original_file="").order_by("pk")
        if limit:
            unseen = unseen[:limit]
        looked = found = unreadable = 0
        for image in unseen.iterator():
            looked += 1
            try:
                image.original_file.open("rb")
                try:
                    with Image.open(image.original_file) as source:
                        position = gps_position(source)
                finally:
                    image.original_file.close()
            except Exception:
                unreadable += 1                                   # not marked: it may be readable next time
                continue
            if position is not None:
                found += 1
            if not dry:
                ImageUpload.all_objects.filter(pk=image.pk).update(
                    gps_lat=position[0] if position else None, gps_lon=position[1] if position else None, location_checked_at=timezone.now(),
                )
        verb = "would read" if dry else "read"
        self.stdout.write(f"Pass 1: looked at {looked} photograph(s), {verb} a position for {found}, {unreadable} could not be read, {looked - found - unreadable} have none.")

        usable, reason = gazetteer.status()
        if not usable:
            self.stdout.write(f"Pass 2 skipped: place names are off ({reason})")
            return
        unplaced = ImageUpload.all_objects.filter(status="uploaded", gps_lat__isnull=False, place__isnull=True).order_by("pk")
        if limit:
            unplaced = unplaced[:limit]
        placed = looked_at = 0
        for image in unplaced.iterator():
            looked_at += 1
            if dry:
                placed += int(gazetteer.nearest(image.gps_lat, image.gps_lon) is not None)
            elif attach_place(image) is not None:
                placed += 1
        self.stdout.write(f"Pass 2: looked at {looked_at} photograph(s) with a position, {'would place' if dry else 'placed'} {placed}.")

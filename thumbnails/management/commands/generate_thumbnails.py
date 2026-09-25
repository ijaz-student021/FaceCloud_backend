from django.core.management.base import BaseCommand, CommandError

from thumbnails.models import ImageThumbnail
from thumbnails.services import allowed_sizes, default_size
from thumbnails.tasks import pregenerate_thumbnail
from uploads.models import ImageUpload


class Command(BaseCommand):
    help = (
        "Generate missing thumbnails for photos uploaded before thumbnails existed "
        "(or after pre-generation was switched off). Safe to run repeatedly."
    )

    def add_arguments(self, parser):
        parser.add_argument("--size", type=int, default=None, help="Longest edge in pixels (default: the smallest configured size).")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many photos (0 = all).")
        parser.add_argument("--dry-run", action="store_true", help="Only count what would be generated.")

    def handle(self, *args, **options):
        size = options["size"] or default_size()
        if size not in allowed_sizes():
            raise CommandError(f"--size must be one of: {', '.join(str(s) for s in allowed_sizes())}.")
        done = ImageThumbnail.objects.filter(size=size).values("image_id")
        pending = (
            ImageUpload.objects.filter(status="uploaded").exclude(original_file="")
            .exclude(pk__in=done).order_by("pk").values_list("pk", flat=True)
        )
        if options["limit"] > 0:
            pending = pending[: options["limit"]]
        ids = list(pending)
        if options["dry_run"]:
            self.stdout.write(f"{len(ids)} photo(s) need a {size}px thumbnail.")
            return
        generated = failed = 0
        for image_id in ids:
            if pregenerate_thumbnail(image_id, size) is None:
                failed += 1
            else:
                generated += 1
        self.stdout.write(self.style.SUCCESS(
            f"Generated {generated} {size}px thumbnail(s); {failed} photo(s) could not be read."
        ))

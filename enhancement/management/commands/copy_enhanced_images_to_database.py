"""Copy every completed enhanced image into the database (table enhancement_enhancedimageblob), or check the copies against the stored files."""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from enhancement.database_copy import store_copy, verify_copy
from enhancement.models import EnhancedImageBlob, EnhancementStatus, ImageEnhancementResult

COPY_LABELS = {
    "stored": "copied into the database",
    "would_store": "would be copied",
    "unchanged": "already copied",
    "encrypted": "not copied: stored images are encrypted and the copy would be plain bytes",
    "size_differs": "not copied: the file is not the size the result recorded",
    "file_missing": "not copied: the stored file is missing",
    "no_file": "no stored file",
}
VERIFY_LABELS = {
    "ok": "identical to the stored file and a valid image of the recorded size",
    "missing": "no copy in the database",
    "file_missing": "the stored file is missing",
    "bytes_differ": "the copy differs from the stored file",
    "not_an_image": "the copy is not a readable image",
    "size_differs": "the image is not the size the result recorded",
}


class Command(BaseCommand):
    help = "Copy every completed enhanced image into the database so a database viewer can open it; --verify checks the copies against the stored files."

    def add_arguments(self, parser):
        parser.add_argument("--verify", action="store_true", help="Write nothing: check every copy against its stored file and exit with an error if any is not perfect.")
        parser.add_argument("--dry-run", action="store_true", help="Write nothing: say what would be copied.")

    def handle(self, *args, **options):
        if options["verify"] and options["dry_run"]:
            raise CommandError("--verify and --dry-run cannot be combined: neither writes anything.")
        results = (
            ImageEnhancementResult.objects.filter(status=EnhancementStatus.COMPLETED).exclude(enhanced_file="")
            .only("id", "enhanced_file", "output_file_size", "output_width", "output_height").order_by("id")
        )
        counts, problems = Counter(), []
        for result in results.iterator():
            if options["verify"]:
                outcome = verify_copy(result)
                if outcome != "ok":
                    problems.append((result.pk, outcome))
            else:
                outcome, _copy = store_copy(result, write=not options["dry_run"])
                if outcome in ("size_differs", "file_missing", "no_file"):
                    problems.append((result.pk, outcome))
            counts[outcome] += 1
        labels = VERIFY_LABELS if options["verify"] else COPY_LABELS
        self.stdout.write(f"Completed results with an enhanced file: {sum(counts.values())}")
        for outcome, number in sorted(counts.items(), key=lambda item: -item[1]):
            self.stdout.write(f"  {number:6d}  {labels[outcome]}")
        for pk, outcome in problems[:20]:
            self.stdout.write(self.style.WARNING(f"  result {pk}: {labels[outcome]}"))
        held = EnhancedImageBlob.objects.aggregate(total=Sum("byte_size"))["total"] or 0
        self.stdout.write(f"Copies now in the database: {EnhancedImageBlob.objects.count()} ({held / 1e6:.1f} MB)")
        if options["verify"] and problems:
            raise CommandError(f"{len(problems)} result(s) have no perfect copy.")

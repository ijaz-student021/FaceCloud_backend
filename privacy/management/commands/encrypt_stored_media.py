from django.core.management.base import BaseCommand, CommandError

from privacy.media_encryption import encrypt_stored_media


class Command(BaseCommand):
    help = (
        "Encrypt stored images that were written before IMAGE_ENCRYPTION_ENABLED was on "
        "and, with --rotate, re-encrypt files written under a retired key. Safe to repeat."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
        parser.add_argument(
            "--rotate", action="store_true",
            help="Also rewrite files whose key id is not the current IMAGE_ENCRYPTION_KEY_ID.",
        )

    def handle(self, *args, **options):
        try:
            summary = encrypt_stored_media(dry_run=options["dry_run"], rotate=options["rotate"])
        except RuntimeError as error:
            raise CommandError(str(error)) from error
        verb = "would be" if summary["dry_run"] else "were"
        self.stdout.write(
            f"Scanned {summary['scanned']} files: {summary['already_current']} already current, "
            f"{summary['encrypted']} {verb} encrypted, {summary['rotated']} {verb} re-keyed, "
            f"{summary['failed']} failed."
        )
        if summary["failed"]:
            raise CommandError(
                f"{summary['failed']} files could not be converted and were left unchanged; see the log."
            )

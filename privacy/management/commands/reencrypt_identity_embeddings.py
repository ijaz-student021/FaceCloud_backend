from django.core.management.base import BaseCommand, CommandError

from privacy.services import reencrypt_identity_embeddings


class Command(BaseCommand):
    help = (
        "Re-protect every stored face embedding and group centroid under the current "
        "IDENTITY_EMBEDDING_KEY / IDENTITY_EMBEDDING_KEY_ID in the current format. Run it "
        "after rotating the key, or once to upgrade legacy FCID1 records."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
        parser.add_argument("--batch-size", type=int, default=500, help="Rows locked and rewritten per transaction.")

    def handle(self, *args, **options):
        if options["batch_size"] <= 0:
            raise CommandError("--batch-size must be positive.")
        stats = reencrypt_identity_embeddings(
            dry_run=options["dry_run"], batch_size=options["batch_size"]
        )
        verb = "would be upgraded" if options["dry_run"] else "upgraded"
        for label in ("faces", "groups"):
            section = stats[label]
            self.stdout.write(
                f"{label}: {section['total']} protected, {section['already_current']} already current, "
                f"{section['upgraded']} {verb}, {section['failed']} could not be decrypted."
            )
        if stats["failed"]:
            raise CommandError(
                f"{stats['failed']} record(s) could not be decrypted with the configured keys and were "
                "left unchanged. Add the missing key to IDENTITY_EMBEDDING_PREVIOUS_KEYS and run again."
            )
        self.stdout.write(self.style.SUCCESS("Dry run complete." if options["dry_run"] else "All stored embeddings use the current key."))

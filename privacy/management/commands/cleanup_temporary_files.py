from django.core.management.base import BaseCommand

from privacy.cleanup import cleanup_temporary_files


class Command(BaseCommand):
    help = (
        "Remove leftover ZIP build files from the temp directory and report (or, when enabled, "
        "delete) media files that no database row references."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; delete nothing.")
        parser.add_argument(
            "--delete-orphans", action="store_true",
            help="Also delete orphaned media files (otherwise they are only reported unless "
                 "ORPHAN_MEDIA_CLEANUP_ENABLED is set).",
        )

    def handle(self, *args, **options):
        summary = cleanup_temporary_files(
            dry_run=options["dry_run"], delete_orphans=True if options["delete_orphans"] else None,
        )
        archives, orphans = summary["temp_archives"], summary["orphaned_media"]
        verb = "found" if summary["dry_run"] else "deleted"
        count = archives["found"] if summary["dry_run"] else archives["deleted"]
        self.stdout.write(
            f"Temp archives: {archives['found']} stale, {count} {verb}, {archives['failed']} failed "
            f"({archives['bytes_found']} bytes found)."
        )
        self.stdout.write(
            f"Orphaned media: {orphans['found']} found ({orphans['bytes_found']} bytes), "
            f"{orphans['deleted']} deleted, {orphans['failed']} failed; deletion "
            f"{'enabled' if orphans['delete_enabled'] else 'disabled (report only)'}."
        )
        for name in orphans["names"]:
            self.stdout.write(f"  orphan: {name}")
        if orphans["found"] > len(orphans["names"]):
            self.stdout.write(f"  ... and {orphans['found'] - len(orphans['names'])} more")

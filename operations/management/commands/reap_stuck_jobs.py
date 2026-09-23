from django.core.management.base import BaseCommand

from operations.jobs import reap_stuck_jobs


class Command(BaseCommand):
    help = (
        "Fail background jobs that are still queued or processing although their worker is gone, "
        "so the owner can retry them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")

    def handle(self, *args, **options):
        summary = reap_stuck_jobs(dry_run=options["dry_run"])
        verb = "would fail" if summary["dry_run"] else "failed"
        self.stdout.write(f"Stuck jobs {verb}: {summary['total']}")
        for kind, count in summary["by_kind"].items():
            self.stdout.write(f"  {kind}: {count}")

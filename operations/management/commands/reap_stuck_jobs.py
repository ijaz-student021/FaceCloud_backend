from django.core.management.base import BaseCommand

from operations.jobs import reap_stuck_jobs


class Command(BaseCommand):
    help = (
        "Fail background jobs that are still queued or processing although their worker is gone, "
        "so the owner can retry them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
        parser.add_argument(
            "--older-than-seconds", type=int, default=None,
            help="Treat a job that has been 'processing' for longer than this as stuck, instead of JOB_PROCESSING_STALE_SECONDS. "
                 "For a development server that was just restarted (a restart drops the job it was running): for example 120.",
        )

    def handle(self, *args, **options):
        summary = reap_stuck_jobs(dry_run=options["dry_run"], processing_seconds=options["older_than_seconds"])
        verb = "would fail" if summary["dry_run"] else "failed"
        self.stdout.write(f"Stuck jobs {verb}: {summary['total']}")
        for kind, count in summary["by_kind"].items():
            self.stdout.write(f"  {kind}: {count}")

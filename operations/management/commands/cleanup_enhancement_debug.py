from django.core.management.base import BaseCommand

from enhancement.adaptive.debug import cleanup


class Command(BaseCommand):
    help = "Delete development debug artifacts of the adaptive enhancement engine that are past their retention."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=float, default=None,
                            help="Override ADAPTIVE_DEBUG_RETENTION_HOURS for this run.")

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {cleanup(options['older_than_hours'])} debug run(s).")

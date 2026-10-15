import json

from django.core.management.base import BaseCommand

from operations.adaptive_metrics import adaptive_metrics


class Command(BaseCommand):
    help = ("Outcome rates, latency (P50/P95), queue wait, rejected operations and their reasons, and models run, for the adaptive "
            "enhancement engine over a time window. Reads stored job records only.")

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(adaptive_metrics(options["days"]), indent=2, default=str))

import json

from django.core.management.base import BaseCommand

from operations.storage_checks import FAIL, run_checks


class Command(BaseCommand):
    help = (
        "Check the storage configuration: with STORAGE_BACKEND=s3, that the credentials are not the account's root "
        "keys, the bucket is private, encrypted and recoverable, and a file can be written, read back and deleted."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print the results as JSON.")

    def handle(self, *args, **options):
        results = run_checks()
        failed = any(status == FAIL for _name, status, _detail in results)
        if options["json"]:
            self.stdout.write(json.dumps([{"check": n, "status": s, "detail": d} for n, s, d in results], indent=2))
        else:
            for name, status, detail in results:
                self.stdout.write(f"{status.upper():5s} {name}: {detail}")
        if failed:
            raise SystemExit(1)

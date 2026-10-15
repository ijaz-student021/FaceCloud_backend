import json

from django.core.management.base import BaseCommand, CommandError

from enhancement.gfpgan import GFPGANUnavailable, gfpgan_healthcheck


class Command(BaseCommand):
    help = (
        "Validate the private GFPGAN checkpoint and optionally initialize "
        "the isolated inference runtime."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--load-model",
            action="store_true",
            help="Import the GFPGAN runtime and initialize the pinned model.",
        )

    def handle(self, *args, **options):
        try:
            metadata = gfpgan_healthcheck(load_model=options["load_model"])
        except GFPGANUnavailable as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(metadata, indent=2, sort_keys=True))
        self.stdout.write(self.style.SUCCESS("GFPGAN preflight passed."))

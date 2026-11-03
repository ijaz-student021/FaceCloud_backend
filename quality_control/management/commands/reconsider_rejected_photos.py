"""Give the photographs that a quality check refused (before quality checks stopped refusing photographs) their second chance.

    python manage.py reconsider_rejected_photos                # list them (dry run: changes nothing)
    python manage.py reconsider_rejected_photos --apply        # re-run them
    python manage.py reconsider_rejected_photos --apply --image 137 --image 125

For each photograph whose stored quality verdict is ``rejected`` (and that is not in the trash, and was uploaded fine) the face check runs again (it now finds a large face in a large
photograph) and so does the quality check (it no longer refuses a photograph; a dark one is no longer read as blurry). The new verdict then does what every verdict does: a person folder,
an automatic enhancement in a personal workspace, a moderator's review in a team. Nothing else is touched; an enhancement that already exists stays.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from quality_control.models import ImageQualityDecision
from quality_control.services import enqueue_quality_analysis
from uploads.models import ImageUpload
from validation.services import validate_image_faces_safely


def rejected_photographs(image_ids=None):
    queryset = ImageUpload.objects.filter(status="uploaded", quality_decision__decision=ImageQualityDecision.Decision.REJECTED, quality_decision__status="completed")
    if image_ids:
        queryset = queryset.filter(pk__in=image_ids)
    return queryset.select_related("quality_decision", "batch").order_by("pk")


class Command(BaseCommand):
    help = "Re-run the face check and the quality check for photographs a quality check refused before quality checks stopped refusing photographs."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Re-run them. Without it nothing is changed: the photographs are only listed.")
        parser.add_argument("--image", type=int, action="append", help="Only this photograph (repeatable).")

    def handle(self, *args, **options):
        photographs = list(rejected_photographs(options.get("image")))
        if not photographs:
            self.stdout.write("No photograph is refused by a quality check.")
            return
        for image in photographs:
            reasons = ", ".join(image.quality_decision.rejection_reasons or []) or "no reason recorded"
            self.stdout.write(f"  photograph {image.pk} {image.original_name!r} (upload {image.batch_id}): refused for {reasons}")
        if not options["apply"]:
            self.stdout.write(f"{len(photographs)} photograph(s) would be re-run. Nothing was changed; add --apply to re-run them.")
            return
        done = failed = 0
        for image in photographs:
            try:
                with transaction.atomic():
                    validate_image_faces_safely(image)
                    enqueue_quality_analysis(image)
                done += 1
            except Exception as error:      # one photograph must not stop the others
                failed += 1
                self.stderr.write(f"  photograph {image.pk}: could not be re-run ({type(error).__name__})")
        self.stdout.write(f"Re-run {done} photograph(s); {failed} could not be re-run.")

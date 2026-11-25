"""Mark the quality decisions that a moderator or an administrator rejected BEFORE ``decided_by_person`` existed, so those photographs leave All/Accepted/In review.

    python manage.py backfill_person_rejections            # dry run: says what it would change
    python manage.py backfill_person_rejections --apply    # does it

Two sources tell who decided: a moderator review (source ``quality``, rejected, with ``decided_by``) and an administrator's ``admin_image_decided`` activity event whose action was
``reject``. Only a decision that is still ``rejected`` is marked (one that was approved or put back under review since is left alone), and the reviewer's reason is written on the
photograph the way a new rejection writes it. Running it twice changes nothing the second time.
"""
from django.core.management.base import BaseCommand

from activity.models import ActivityEvent
from moderation.models import ModeratorReview
from moderation.services import REVIEWER_REJECTION_PREFIX
from quality_control.models import ImageQualityDecision
from uploads.models import ImageUpload


class Command(BaseCommand):
    help = "Mark quality decisions rejected by a moderator or an administrator before the decided_by_person flag existed."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (without it nothing is written).")

    def handle(self, *args, **options):
        reasons = {}            # image id -> (who, reason)
        for review in ModeratorReview.objects.filter(source=ModeratorReview.Source.QUALITY, status=ModeratorReview.Status.REJECTED, decided_by__isnull=False).select_related("decided_by"):
            reasons[review.image_id] = ("a moderator", review.decision_reason or "")
        for event in ActivityEvent.objects.filter(event="admin_image_decided", object_type="ImageUpload"):
            details = event.details or {}
            if details.get("action") == "reject" and str(event.object_id).isdigit():
                reasons[int(event.object_id)] = ("an administrator", details.get("reason", ""))
        todo = ImageQualityDecision.objects.filter(image_id__in=list(reasons), decision=ImageQualityDecision.Decision.REJECTED, decided_by_person=False)
        count = todo.count()
        self.stdout.write(f"{count} rejected quality decision(s) were written by a person and are not marked yet.")
        if not options["apply"]:
            self.stdout.write("Dry run: nothing was written. Add --apply to mark them.")
            return
        for decision in todo:
            who, reason = reasons[decision.image_id]
            ImageQualityDecision.objects.filter(pk=decision.pk).update(decided_by_person=True)
            ImageUpload.objects.filter(pk=decision.image_id, rejection_reason="").update(rejection_reason=f"{REVIEWER_REJECTION_PREFIX}{who}" + (f": {reason}" if reason else "."))
        self.stdout.write(f"Marked {count}.")

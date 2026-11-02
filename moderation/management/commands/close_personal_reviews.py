"""Close the reviews that were opened in personal workspaces, where nobody can decide them.

    python manage.py close_personal_reviews            # dry run: says what it would close, changes nothing
    python manage.py close_personal_reviews --apply    # closes them

A personal workspace has no moderator. Before this was fixed, a photograph the checks were unsure about opened a review there that stayed pending for ever and was counted in the
administrator's moderation totals. New ones are no longer opened (``moderation.services``); this closes the ones that exist. A closed review keeps its row: status ``rejected`` (the
review was closed without a decision, as ``close_open_identity_reviews`` does), the reason "No moderator in a personal workspace", the time it was closed, and **no deciding moderator**
(``decided_by`` stays empty, which is how a closing by the system is told from a moderator's decision). The photograph and its quality verdict are not touched. Safe to run again:
it only looks at reviews that are still open.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from moderation.models import ModeratorReview
from moderation.services import OPEN_REVIEW_STATUSES

REASON = "No moderator in a personal workspace"


def open_personal_reviews():
    return ModeratorReview.objects.filter(organization__is_personal=True, status__in=OPEN_REVIEW_STATUSES, decided_at__isnull=True)


class Command(BaseCommand):
    help = "Close the pending reviews of personal workspaces (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Close them. Without it nothing is changed.")

    def handle(self, *args, **options):
        queryset = open_personal_reviews()
        counts = Counter(queryset.values_list("source", "status"))
        total = sum(counts.values())
        for (source, status), number in sorted(counts.items()):
            self.stdout.write(f"  {number} {source} review(s) {status}")
        if not options["apply"]:
            self.stdout.write(f"Dry run: {total} open review(s) in personal workspaces would be closed. Run with --apply to close them.")
            return
        now = timezone.now()
        with transaction.atomic():
            closed = open_personal_reviews().update(
                status=ModeratorReview.Status.REJECTED, decision_reason=REASON, decided_at=now, decided_by=None,
                claimed_by=None, claimed_at=None, updated_at=now,
            )
        self.stdout.write(self.style.SUCCESS(f"Closed {closed} review(s) with the reason \"{REASON}\"."))

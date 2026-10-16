from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from accounts.models import PendingRegistration


class Command(BaseCommand):
    help = "Delete expired or completed signup registrations past retention."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(
            hours=settings.SIGNUP_VERIFICATION_RETENTION_HOURS
        )
        stale_unconsumed = Q(
            status__in=[
                PendingRegistration.STATUS_PENDING,
                PendingRegistration.STATUS_LOCKED,
                PendingRegistration.STATUS_EXPIRED,
                PendingRegistration.STATUS_CANCELLED,
            ],
            token_expires_at__lt=cutoff,
        )
        stale_consumed = Q(
            status=PendingRegistration.STATUS_CONSUMED,
            updated_at__lt=cutoff,
        )
        deleted, _ = PendingRegistration.objects.filter(
            stale_unconsumed | stale_consumed
        ).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} pending registration records."
            )
        )

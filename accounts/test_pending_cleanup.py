"""Abandoned signups are cleaned up on a schedule, not only when someone remembers to run the command."""
from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from config.celery import app

from .models import PendingRegistration
from .tasks import cleanup_pending_registrations


@override_settings(SIGNUP_VERIFICATION_RETENTION_HOURS=24)
class PendingRegistrationCleanupTests(TestCase):
    def make(self, name, status, *, expired_hours_ago=None, touched_hours_ago=0):
        now = timezone.now()
        expires = now - timedelta(hours=expired_hours_ago) if expired_hours_ago is not None else now + timedelta(hours=20)
        row = PendingRegistration.objects.create(
            username=name, email=f"{name}@example.com", password_hash="x", token_hash="x", status=status,
            resend_window_started_at=now, token_expires_at=expires, resend_available_at=now)
        PendingRegistration.objects.filter(pk=row.pk).update(updated_at=now - timedelta(hours=touched_hours_ago))
        return row

    def test_it_is_on_the_beat_schedule_and_registered(self):
        entry = settings.CELERY_BEAT_SCHEDULE["purge-pending-registrations"]
        self.assertEqual(entry["task"], "accounts.cleanup_pending_registrations")
        self.assertEqual((sorted(entry["schedule"].hour), sorted(entry["schedule"].minute)), ([3], [55]))   # daily, in the quiet hours
        app.loader.import_default_modules()
        self.assertIn("accounts.cleanup_pending_registrations", app.tasks)

    def test_the_task_removes_an_old_abandoned_registration_and_keeps_what_is_recent(self):
        old = self.make("old", PendingRegistration.STATUS_PENDING, expired_hours_ago=48)
        old_locked = self.make("lockedold", PendingRegistration.STATUS_LOCKED, expired_hours_ago=30)
        recent = self.make("recent", PendingRegistration.STATUS_PENDING)                       # link still valid
        just_expired = self.make("justexpired", PendingRegistration.STATUS_EXPIRED, expired_hours_ago=2)
        consumed_recent = self.make("done", PendingRegistration.STATUS_CONSUMED, touched_hours_ago=3)
        consumed_old = self.make("doneold", PendingRegistration.STATUS_CONSUMED, touched_hours_ago=72)
        removed = cleanup_pending_registrations()
        self.assertEqual(removed, 3)
        remaining = set(PendingRegistration.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {recent.pk, just_expired.pk, consumed_recent.pk})
        self.assertFalse({old.pk, old_locked.pk, consumed_old.pk} & remaining)

    def test_the_command_and_the_task_are_the_same_code(self):
        self.make("old", PendingRegistration.STATUS_PENDING, expired_hours_ago=48)
        out = StringIO()
        call_command("cleanup_pending_registrations", stdout=out)
        self.assertIn("Deleted 1 pending registration", out.getvalue())
        self.assertEqual(cleanup_pending_registrations(), 0)

    @override_settings(SIGNUP_VERIFICATION_RETENTION_HOURS=1)
    def test_the_retention_setting_is_honoured(self):
        self.make("recentish", PendingRegistration.STATUS_PENDING, expired_hours_ago=2)
        self.assertEqual(cleanup_pending_registrations(), 1)

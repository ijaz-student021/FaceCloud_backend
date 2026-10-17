"""Every "open" link a notification carries must be a page the frontend has."""
import importlib
import re
from pathlib import Path

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from uploads.models import UploadBatch

from . import events
from .models import UserNotification

User = get_user_model()

# The frontend's user and moderator portal routes (src/App.tsx) that notifications may open.
FRONTEND_ROUTES = {
    "/dashboard", "/dashboard/upload", "/dashboard/images", "/dashboard/groups", "/dashboard/downloads",
    "/dashboard/notifications", "/dashboard/activity", "/dashboard/settings", "/dashboard/jobs",
    "/moderator", "/moderator/queue", "/moderator/batches", "/moderator/notifications",
}


class ActionPathTests(TestCase):
    def test_every_path_written_in_events_is_a_frontend_route(self):
        source = Path(events.__file__).read_text()
        paths = set(re.findall(r'(?:action_path\s*[=:]\s*)"(/[^"]*)"', source))
        paths |= {events.JOBS_PATH, events.GROUPS_PATH}
        self.assertTrue(paths)
        self.assertEqual(paths - FRONTEND_ROUTES, set())

    def test_the_upload_notifications_open_your_uploads(self):
        user = User.objects.create_user(
            username="paths", email="paths@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=user, upload_type="multiple", total_files=2)
        events.notify_upload_received(batch.pk)
        events.notify_processing_started(batch.pk)
        paths = set(UserNotification.objects.filter(recipient=user).values_list("action_path", flat=True))
        self.assertEqual(paths, {"/dashboard/jobs"})

    def test_the_migration_repairs_rows_written_with_the_old_paths(self):
        user = User.objects.create_user(
            username="paths-old", email="paths-old@example.com", password="ForestRiver!4821", is_email_verified=True,
        )
        for index, path in enumerate(("/dashboard/uploads", "/dashboard/person-folders", "/dashboard/images")):
            UserNotification.objects.create(
                recipient=user, event_type="upload_received", idempotency_key=f"old-{index}",
                title="t", message="m", severity="info", action_path=path,
            )
        migration = importlib.import_module("notifications.migrations.0005_fix_action_paths")
        migration.fix_paths(django_apps, None)
        paths = sorted(UserNotification.objects.values_list("action_path", flat=True))
        self.assertEqual(paths, ["/dashboard/groups", "/dashboard/images", "/dashboard/jobs"])

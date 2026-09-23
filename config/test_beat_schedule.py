from django.conf import settings
from django.test import SimpleTestCase

from config.celery import app


class BeatScheduleTests(SimpleTestCase):
    """A misspelled task name in a beat schedule only fails at run time."""

    def test_every_scheduled_task_is_a_registered_task(self):
        app.loader.import_default_modules()
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            with self.subTest(entry=name):
                self.assertIn(entry["task"], app.tasks)

    def test_stuck_jobs_are_checked_every_five_minutes(self):
        entry = settings.CELERY_BEAT_SCHEDULE["reap-stuck-jobs"]
        self.assertEqual(sorted(entry["schedule"].minute), list(range(0, 60, 5)))

    def test_temp_cleanup_runs_more_often_than_the_temp_file_age_limit(self):
        entry = settings.CELERY_BEAT_SCHEDULE["cleanup-temporary-files"]
        hours = sorted(entry["schedule"].hour)
        gaps = [b - a for a, b in zip(hours, hours[1:] + [hours[0] + 24])]
        self.assertLess(max(gaps), settings.TEMP_FILE_MAX_AGE_HOURS)

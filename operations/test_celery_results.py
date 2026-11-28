"""Nothing reads a Celery task's return value, so no task keeps one.

Every FaceCloud task writes its outcome to its own table and the screens poll that table. A task that kept its result made the web process subscribe to
the Redis result backend whenever it sent the task, and a Redis outage then held each upload request for about 58 seconds (20 reconnect attempts of the
result consumer) before the failure was recorded. Ignoring results makes the publish itself the only thing that can fail, and it fails within seconds.
"""
from django.conf import settings
from django.test import TestCase

from config.celery import app


class CeleryResultsTests(TestCase):
    def test_results_are_ignored_by_default(self):
        self.assertTrue(settings.CELERY_TASK_IGNORE_RESULT)
        self.assertTrue(app.conf.task_ignore_result)

    def test_no_facecloud_task_asks_to_keep_its_result(self):
        app.loader.import_default_modules()
        own = [task for name, task in app.tasks.items() if not name.startswith("celery.")]
        self.assertGreater(len(own), 10)                                  # the project's tasks were found
        kept = sorted(task.name for task in own if not task.ignore_result)
        self.assertEqual(kept, [], "these tasks keep their return value; nothing reads it (see the module docstring)")

    def test_nothing_in_the_project_reads_a_task_result(self):
        import subprocess
        from pathlib import Path

        root = Path(settings.BASE_DIR)
        found = subprocess.run(
            ["git", "grep", "-n", "-E", r"AsyncResult|\.get\(timeout=|GroupResult", "--", "*.py", ":!*/test*", ":!operations/test_celery_results.py"],
            cwd=root, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(found, "", f"code reads a task result, so ignoring results would break it:\n{found}")

"""Background jobs running inside the request (``CELERY_TASK_ALWAYS_EAGER``) is made visible: a startup warning, a system check, the health endpoint and every upload answer."""
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .checks import background_jobs_run_inline
from .inline import background_jobs_inline, processing_mode, warn_if_inline
from .services import HEALTHY, build_system_health

User = get_user_model()


class InlineModeSwitchTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_eager_means_inline(self):
        self.assertTrue(background_jobs_inline())
        self.assertEqual(processing_mode(), "inline")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_not_eager_means_background(self):
        self.assertFalse(background_jobs_inline())
        self.assertEqual(processing_mode(), "background")


class StartupWarningTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_a_clear_warning_is_logged_when_jobs_run_inside_the_request(self):
        with self.assertLogs("operations.inline", level="WARNING") as logged:
            self.assertTrue(warn_if_inline())
        (line,) = logged.output
        self.assertIn("Background jobs run inside the request", line)
        self.assertIn("Start a Celery worker", line)
        self.assertIn("CELERY_TASK_ALWAYS_EAGER=False", line)
        self.assertIn("see progress", line)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_nothing_is_logged_when_a_worker_does_the_work(self):
        with self.assertNoLogs("operations.inline", level="WARNING"):
            self.assertFalse(warn_if_inline())

    def test_both_web_entry_points_say_it_when_the_server_loads(self):
        import importlib
        import sys

        for module in ("config.wsgi", "config.asgi"):
            sys.modules.pop(module, None)                       # so that importing it runs it once, as a server starting would
            with self.subTest(module), patch("operations.inline.warn_if_inline") as warn:
                importlib.import_module(module)
            warn.assert_called_once_with()


class SystemCheckTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_a_warning_not_an_error_when_eager(self):
        (message,) = background_jobs_run_inline(None)
        self.assertEqual((message.id, message.level), ("operations.W001", 30))        # 30 = WARNING: it never stops the server starting
        self.assertIn("CELERY_TASK_ALWAYS_EAGER=False", message.msg)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_nothing_when_not_eager(self):
        self.assertEqual(background_jobs_run_inline(None), [])


class HealthTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="inline-admin", email="inline-admin@example.com", password="Password!123", role="admin", is_email_verified=True)
        self.client = APIClient()
        token = RefreshToken.for_user(self.admin)
        token["role"] = "admin"
        token["token_version"] = self.admin.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def health(self, workers):
        quiet = {"status": HEALTHY, "latency_ms": 0.1}
        with patch.dict("operations.services.CHECKS", {"api": lambda: quiet, "database": lambda: quiet, "redis": lambda: quiet, "storage": lambda: quiet}, clear=True), \
                patch("operations.services._inspect_workers", return_value={"status": HEALTHY, "latency_ms": 0.2, "workers": workers}), \
                patch("operations.services.job_counts", return_value={"pending": 0, "active": 0, "failed": 0}):
            from operations.services import check_celery

            with patch.dict("operations.services.CHECKS", {"celery": check_celery}):
                return self.client.get(reverse("operations:system-health"))

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_the_administrator_is_told_jobs_run_inline_and_the_celery_service_says_eager(self):
        response = self.health(workers=0)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIs(body["background_jobs_inline"], True)
        self.assertIs(body["checks"]["celery"]["eager"], True)
        celery = next(row for row in body["services"] if row["key"] == "celery")
        self.assertEqual(celery["detail"], "eager")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_with_a_worker_the_flag_is_false_and_the_service_has_no_eager_detail(self):
        body = self.health(workers=1).json()
        self.assertIs(body["background_jobs_inline"], False)
        self.assertNotIn("eager", body["checks"]["celery"])
        celery = next(row for row in body["services"] if row["key"] == "celery")
        self.assertEqual(celery["detail"], "")
        self.assertEqual(celery["workers"], 1)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_nothing_that_was_there_is_removed(self):
        body = self.health(workers=0).json()
        for key in ("status", "version", "checked_at", "response_time_ms", "checks", "jobs", "services", "process"):
            self.assertIn(key, body)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_the_service_function_reports_it_too(self):
        quiet = {"status": HEALTHY, "latency_ms": 0.1}
        required = {"database": lambda: quiet, "redis": lambda: quiet, "storage": lambda: quiet}
        with patch.dict("operations.services.CHECKS", required, clear=True), patch("operations.services.job_counts", return_value={"pending": 0, "active": 0, "failed": 0}):
            self.assertIs(build_system_health()["background_jobs_inline"], True)


class UploadAnswerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="inline-uploader", email="inline-uploader@example.com", password="Password!123", is_email_verified=True)
        self.client = APIClient()
        token = RefreshToken.for_user(self.user)
        token["role"] = self.user.role
        token["token_version"] = self.user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def png(self, name="face.png"):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 130, 140)).save(buffer, format="PNG")
        buffer.seek(0)
        buffer.name = name
        return buffer

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_a_single_upload_says_it_ran_inline(self):
        response = self.client.post("/api/images/upload/single/", {"file": self.png()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["processing_mode"], "inline")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_a_single_upload_says_a_worker_does_the_work(self):
        response = self.client.post("/api/images/upload/single/", {"file": self.png()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["processing_mode"], "background")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_multiple_and_zip_uploads_carry_it_too(self):
        import zipfile

        with patch("uploads.views.enqueue_batch_preprocessing", side_effect=lambda batch: batch):
            multiple = self.client.post("/api/images/upload/multiple/", {"files": [self.png("a.png"), self.png("b.png")]}, format="multipart")
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("a.png", self.png("a.png").getvalue())
            archive.seek(0)
            archive.name = "photos.zip"
            zipped = self.client.post("/api/images/upload/zip/", {"file": archive}, format="multipart")
        for response in (multiple, zipped):
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.json()["processing_mode"], "background")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_the_other_batch_endpoints_are_unchanged(self):
        created = self.client.post("/api/images/upload/single/", {"file": self.png()}, format="multipart").json()
        detail = self.client.get(reverse("upload-batch-detail", kwargs={"pk": created["id"]})).json()
        self.assertNotIn("processing_mode", detail)
        for key in created:
            if key != "processing_mode":
                self.assertIn(key, detail)

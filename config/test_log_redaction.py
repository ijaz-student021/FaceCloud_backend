import logging
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from config.log_redaction import REDACTED, redact

TOKEN = "Zk3v9pQ2xLm8RtYw4Hn7BcD1eF6gJ0aSuVoXiKlMnOq"


class RedactFunctionTests(SimpleTestCase):
    def test_the_token_is_replaced_and_the_route_kept(self):
        self.assertEqual(
            redact(f"Not Found: /api/shares/public/{TOKEN}/"),
            f"Not Found: /api/shares/public/{REDACTED}/",
        )

    def test_info_endpoint_query_strings_and_full_urls_are_covered(self):
        cases = {
            f"/api/shares/public/{TOKEN}/info/": f"/api/shares/public/{REDACTED}/info/",
            f"/api/shares/public/{TOKEN}?x=1": f"/api/shares/public/{REDACTED}?x=1",
            f"https://api.example.com/api/shares/public/{TOKEN}/": f"https://api.example.com/api/shares/public/{REDACTED}/",
            f'"GET /api/shares/public/{TOKEN}/ HTTP/1.1" 200 512': f'"GET /api/shares/public/{REDACTED}/ HTTP/1.1" 200 512',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(redact(original), expected)

    def test_several_tokens_in_one_line_are_all_replaced(self):
        line = f"/api/shares/public/{TOKEN}/ then /api/shares/public/AnotherTokenValue123/"
        self.assertNotIn(TOKEN, redact(line))
        self.assertNotIn("AnotherTokenValue123", redact(line))

    def test_everything_else_is_untouched(self):
        for text in (
            "GET /api/images/my/ 200",
            "/api/shares/ list",
            "/api/shares/12/revoke/",
            "public share links are described here",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_an_email_change_verification_link_has_both_segments_redacted(self):
        uidb64 = "MQ"
        cases = {
            f"/api/auth/verify-email/change/{uidb64}/{TOKEN}/": f"/api/auth/verify-email/change/{REDACTED}",
            f"Not Found: /api/auth/verify-email/change/{uidb64}/{TOKEN}/": f"Not Found: /api/auth/verify-email/change/{REDACTED}",
            f"/api/auth/verify-email/change/{uidb64}/{TOKEN}?x=1": f"/api/auth/verify-email/change/{REDACTED}?x=1",
            f'"GET /api/auth/verify-email/change/{uidb64}/{TOKEN}/ HTTP/1.1" 200 512': f'"GET /api/auth/verify-email/change/{REDACTED} HTTP/1.1" 200 512',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(redact(original), expected)
                self.assertNotIn(uidb64, redact(original))
                self.assertNotIn(TOKEN, redact(original))


class LogRecordRedactionTests(SimpleTestCase):
    """The factory is installed when settings load, so these use real loggers."""

    def logged(self, logger_name, message, *args):
        logger = logging.getLogger(logger_name)
        with self.assertLogs(logger_name, level="DEBUG") as captured:
            logger.warning(message, *args)
        return captured.output[0]

    def test_a_token_in_the_message_is_redacted(self):
        output = self.logged("some.app", f"failed for /api/shares/public/{TOKEN}/")
        self.assertNotIn(TOKEN, output)
        self.assertIn(REDACTED, output)

    def test_a_token_in_a_log_argument_is_redacted(self):
        output = self.logged("some.app", "Not Found: %s", f"/api/shares/public/{TOKEN}/")
        self.assertNotIn(TOKEN, output)
        self.assertIn("Not Found:", output)

    def test_a_request_object_argument_is_redacted(self):
        request = SimpleNamespace(path=f"/api/shares/public/{TOKEN}/", __str__=None)
        output = self.logged("some.app", "problem with %s", f"<Request {request.path}>")
        self.assertNotIn(TOKEN, output)

    def test_named_arguments_are_redacted_too(self):
        logger = logging.getLogger("some.app")
        with self.assertLogs("some.app", level="DEBUG") as captured:
            logger.warning("path=%(path)s", {"path": f"/api/shares/public/{TOKEN}/"})
        self.assertNotIn(TOKEN, captured.output[0])

    def test_ordinary_logs_keep_their_arguments_untouched(self):
        output = self.logged("some.app", "processed %d images for %s", 3, "alice")
        self.assertTrue(output.endswith("processed 3 images for alice"))

    def test_the_record_factory_never_raises_on_odd_arguments(self):
        factory = logging.getLogRecordFactory()
        marker = "/api/shares/public/"
        odd = (
            ("mismatched %s %s " + marker, ("only-one",)),   # formatting would fail
            ("object %s " + marker, (object(),)),
            ("plain " + marker + "tok", None),
        )
        for message, args in odd:
            with self.subTest(message=message):
                record = factory("some.app", logging.WARNING, __file__, 1, message, args, None)
                self.assertIsNotNone(record)


class EndToEndTests(TestCase):
    """Hit the real endpoints and inspect what Django actually logs."""

    def test_an_unknown_link_does_not_write_its_token_to_the_request_log(self):
        with self.assertLogs("django.request", level="WARNING") as captured:
            response = APIClient().get(f"/api/shares/public/{TOKEN}/")

        self.assertEqual(response.status_code, 404)
        joined = "\n".join(captured.output)
        self.assertIn("/api/shares/public/", joined)
        self.assertNotIn(TOKEN, joined)
        self.assertIn(REDACTED, joined)

    def test_an_expired_link_and_the_info_route_are_covered_as_well(self):
        from datetime import timedelta

        from django.contrib.auth import get_user_model
        from django.utils import timezone

        from enhancement.models import ImageEnhancementResult
        from sharing.models import ShareLink
        from sharing.services import hash_token
        from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

        user = get_user_model().objects.create_user(
            username="log-owner", email="log-owner@example.com", password="ForestRiver!4821",
            is_email_verified=True,
        )
        batch = UploadBatch.objects.create(owner=user, upload_type="single")
        image = ImageUpload.objects.create(owner=user, batch=batch, original_name="a.png", status="uploaded")
        pre = ImagePreprocessingResult.objects.create(owner=user, image=image, status="completed", output_format="png")
        result = ImageEnhancementResult.objects.create(
            owner=user, image=image, preprocessing_result=pre, status="completed",
        )
        ShareLink.objects.create(
            owner=user, enhancement_result=result, token_hash=hash_token(TOKEN),
            expires_at=timezone.now() - timedelta(hours=1),
        )

        with self.assertLogs("django.request", level="WARNING") as captured:
            APIClient().get(f"/api/shares/public/{TOKEN}/")
            APIClient().get(f"/api/shares/public/{TOKEN}/info/")

        joined = "\n".join(captured.output)
        self.assertIn("Gone", joined)
        self.assertNotIn(TOKEN, joined)

    def test_the_development_server_log_line_is_redacted(self):
        line = f'"GET /api/shares/public/{TOKEN}/ HTTP/1.1" 404 42'
        with self.assertLogs("django.server", level="INFO") as captured:
            logging.getLogger("django.server").info(line)

        self.assertNotIn(TOKEN, captured.output[0])
        self.assertIn("HTTP/1.1", captured.output[0])


class GunicornAccessLogTests(SimpleTestCase):
    def test_the_access_log_atoms_are_redacted(self):
        try:
            from gunicorn.glogging import Logger  # needs a POSIX system
        except (ImportError, ModuleNotFoundError):
            self.skipTest("gunicorn is not importable on this platform")
        import importlib.util
        from pathlib import Path
        from unittest.mock import patch

        from django.conf import settings

        spec = importlib.util.spec_from_file_location("gunicorn_conf", Path(settings.BASE_DIR) / "gunicorn.conf.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        atoms = {"r": f"GET /api/shares/public/{TOKEN}/ HTTP/1.1", "U": f"/api/shares/public/{TOKEN}/", "s": "200"}
        with patch.object(Logger, "atoms", return_value=atoms):
            redacted = module.RedactingLogger.atoms(module.RedactingLogger.__new__(module.RedactingLogger), None, None, {}, 0)

        self.assertNotIn(TOKEN, str(redacted))
        self.assertEqual(redacted["s"], "200")

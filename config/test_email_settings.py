"""The SMTP backend gets a timeout (EMAIL_TIMEOUT), so a slow or unreachable mail server cannot hang a sign-up request until the operating system gives up."""
from django.conf import settings
from django.core.mail import get_connection
from django.test import SimpleTestCase, override_settings

SMTP = "django.core.mail.backends.smtp.EmailBackend"


class EmailTimeoutTests(SimpleTestCase):
    def test_the_default_is_ten_seconds(self):
        self.assertEqual(settings.EMAIL_TIMEOUT, 10)

    def test_the_smtp_backend_waits_that_long_and_no_longer(self):
        self.assertEqual(get_connection(backend=SMTP).timeout, settings.EMAIL_TIMEOUT)

    @override_settings(EMAIL_TIMEOUT=3)
    def test_it_can_be_changed(self):
        self.assertEqual(get_connection(backend=SMTP).timeout, 3)

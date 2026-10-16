"""Public contact form (B07): real delivery, honest failure states, abuse limits."""
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import ContactMessage
from .services import purge_old_messages

VALID = {
    "name": "Sara Khan", "email": "sara@example.org", "company": "North Studio",
    "reason": "Technical support", "message": "The ZIP upload stops at 40 percent every time.",
}


@override_settings(CONTACT_RECIPIENT_EMAILS=["team@facecloud.test"], CELERY_TASK_ALWAYS_EAGER=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ContactFormTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle counters
        self.client = APIClient()

    def post(self, body, **headers):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/contact/", body, format="json", **headers)

    def test_a_valid_message_is_accepted_and_delivered_to_the_team(self):
        response = self.post(VALID)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["status"], "queued")
        message = ContactMessage.objects.get()
        self.assertEqual((message.delivery_status, message.delivery_attempts), ("sent", 1))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual((sent.to, sent.reply_to), (["team@facecloud.test"], ["sara@example.org"]))
        self.assertIn("ZIP upload stops", sent.body)

    def test_no_authentication_is_needed_and_a_bad_token_is_ignored(self):
        response = self.post(VALID, HTTP_AUTHORIZATION="Bearer not-a-token")
        self.assertEqual(response.status_code, 202)

    def test_invalid_messages_are_refused(self):
        for change in ({"message": "too short"}, {"email": "nope"}, {"reason": "Spam"}, {"name": ""},
                       {"message": "x" * 5001}):
            with self.subTest(change=change):
                self.assertEqual(self.post({**VALID, **change}).status_code, 400)
        self.assertFalse(ContactMessage.objects.exists())

    def test_the_spam_trap_is_answered_like_success_but_nothing_is_kept_or_sent(self):
        response = self.post({**VALID, "website": "http://spam.example"})
        self.assertEqual(response.status_code, 202)
        self.assertFalse(ContactMessage.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(CONTACT_RECIPIENT_EMAILS=[])
    def test_without_a_recipient_the_form_says_it_is_unavailable(self):
        response = self.post(VALID)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(ContactMessage.objects.exists())

    def test_submissions_are_rate_limited_per_client(self):
        codes = [self.post(VALID).status_code for _ in range(6)]
        self.assertEqual(codes, [202] * 5 + [429])

    def test_a_delivery_failure_is_recorded_not_hidden(self):
        with patch("contact.tasks.deliver", side_effect=OSError("SMTP unreachable")):
            with self.assertLogs("contact.tasks", level="ERROR"):
                response = self.post(VALID)
        self.assertEqual(response.status_code, 202)  # accepted; delivery is asynchronous
        message = ContactMessage.objects.get()
        self.assertEqual(message.delivery_status, "failed")
        self.assertIn("SMTP unreachable", message.failure_reason)

    def test_old_messages_are_purged(self):
        self.post(VALID)
        ContactMessage.objects.update(created_at=timezone.now() - timedelta(days=200))
        self.assertEqual(purge_old_messages(), 1)
        self.assertFalse(ContactMessage.objects.exists())

    def test_only_post_is_offered(self):
        self.assertEqual(self.client.get("/api/contact/").status_code, 405)

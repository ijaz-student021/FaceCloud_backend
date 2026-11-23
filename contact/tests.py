"""Public contact form (B07): real delivery, honest failure states, abuse limits."""
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from django.contrib.auth import get_user_model

from notifications.models import UserNotification

from .models import ContactMessage, EarlyAccessSignup
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
    def test_without_a_recipient_the_message_is_still_stored_and_shown_to_the_administrators_just_not_e_mailed(self):
        response = self.post(VALID)
        self.assertEqual(response.status_code, 202)
        message = ContactMessage.objects.get()
        self.assertEqual((message.delivery_status, message.delivery_attempts), ("skipped", 0))
        self.assertEqual(len(mail.outbox), 0)

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


User = get_user_model()


@override_settings(CONTACT_RECIPIENT_EMAILS=["team@facecloud.test"], CELERY_TASK_ALWAYS_EAGER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdminInboxTests(TestCase):
    """The administrators see what was filled in (an inbox and a notification); other people see nothing."""

    def setUp(self):
        cache.clear()
        self.anonymous = APIClient()
        self.admin = User.objects.create_user(username="boss", email="boss@example.com", password="Password!123", role="admin", is_email_verified=True)
        self.other_admin = User.objects.create_user(username="boss2", email="boss2@example.com", password="Password!123", role="admin", is_email_verified=True)
        self.member = User.objects.create_user(username="member", email="member@example.com", password="Password!123", is_email_verified=True)
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(self.admin)
        self.client_member = APIClient()
        self.client_member.force_authenticate(self.member)

    def submit(self, body=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.anonymous.post("/api/contact/", body or VALID, format="json")

    def test_a_message_is_stored_with_everything_that_was_filled_in_and_each_administrator_is_notified(self):
        self.submit()
        message = ContactMessage.objects.get()
        self.assertEqual((message.name, message.email, message.company, message.reason), ("Sara Khan", "sara@example.org", "North Studio", "Technical support"))
        notes = UserNotification.objects.filter(event_type="contact_message_received")
        self.assertEqual(set(notes.values_list("recipient__username", flat=True)), {"boss", "boss2"})          # the administrators, not the member
        note = notes.get(recipient=self.admin)
        self.assertIn("Sara Khan", note.message)
        self.assertEqual((note.resource_type, note.resource_id, note.action_path), ("contact", message.pk, f"/admin/inbox?kind=contact&id={message.pk}"))

    def test_the_admin_sees_the_full_text_and_can_mark_it_read(self):
        self.submit()
        listed = self.client_admin.get("/api/contact/admin/messages/").json()
        self.assertEqual(listed["count"], 1)
        row = listed["results"][0]
        self.assertEqual((row["name"], row["email"], row["company"], row["reason"], row["message"], row["is_read"]), ("Sara Khan", "sara@example.org", "North Studio", "Technical support", VALID["message"], False))
        self.assertEqual(self.client_admin.get("/api/contact/admin/summary/").json()["contact_unread"], 1)
        marked = self.client_admin.patch(f"/api/contact/admin/messages/{row['id']}/", {"read": True}, format="json").json()
        self.assertTrue(marked["is_read"])
        self.assertEqual(self.client_admin.get("/api/contact/admin/summary/").json()["contact_unread"], 0)
        self.assertEqual(self.client_admin.get("/api/contact/admin/messages/?unread=1").json()["count"], 0)
        self.client_admin.patch(f"/api/contact/admin/messages/{row['id']}/", {"read": False}, format="json")
        self.assertEqual(self.client_admin.get("/api/contact/admin/messages/?unread=1").json()["count"], 1)

    def test_search_finds_a_message_by_what_was_written(self):
        self.submit()
        self.submit({**VALID, "name": "Omar", "email": "omar@example.org", "company": "Other Co", "message": "How long are photographs kept after I delete them?"})
        self.assertEqual(self.client_admin.get("/api/contact/admin/messages/?q=photographs kept").json()["count"], 1)
        self.assertEqual(self.client_admin.get("/api/contact/admin/messages/?q=north studio").json()["count"], 1)
        self.assertEqual(self.client_admin.get("/api/contact/admin/messages/").json()["count"], 2)

    def test_only_administrators_can_read_the_inbox(self):
        self.submit()
        for url in ("/api/contact/admin/messages/", "/api/contact/admin/summary/", "/api/contact/admin/early-access/", f"/api/contact/admin/messages/{ContactMessage.objects.get().pk}/"):
            with self.subTest(url):
                self.assertEqual(self.anonymous.get(url).status_code, 401)
                self.assertEqual(self.client_member.get(url).status_code, 403)

    def test_the_spam_trap_notifies_nobody(self):
        self.submit({**VALID, "website": "http://spam.example"})
        self.assertFalse(UserNotification.objects.filter(event_type="contact_message_received").exists())

    def test_an_early_access_address_is_stored_once_and_the_administrators_are_notified(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.anonymous.post("/api/contact/early-access/", {"email": "Visitor@Example.org"}, format="json")
        self.assertEqual((first.status_code, first.json()["status"]), (201, "registered"))
        with self.captureOnCommitCallbacks(execute=True):
            again = self.anonymous.post("/api/contact/early-access/", {"email": "visitor@example.org"}, format="json")
        self.assertEqual((again.status_code, again.json()["status"]), (200, "already_registered"))
        self.assertEqual(EarlyAccessSignup.objects.count(), 1)
        notes = UserNotification.objects.filter(event_type="early_access_signup")
        self.assertEqual(notes.count(), 2)                                         # one per administrator, and none for the repeat
        self.assertIn("Visitor@Example.org", notes.first().message)
        listed = self.client_admin.get("/api/contact/admin/early-access/").json()
        self.assertEqual((listed["count"], listed["results"][0]["email"], listed["results"][0]["is_read"]), (1, "Visitor@Example.org", False))

    def test_early_access_refuses_a_bad_address_and_ignores_the_spam_trap_and_is_rate_limited(self):
        self.assertEqual(self.anonymous.post("/api/contact/early-access/", {"email": "nope"}, format="json").status_code, 400)
        trap = self.anonymous.post("/api/contact/early-access/", {"email": "bot@example.org", "website": "x"}, format="json")
        self.assertEqual(trap.status_code, 201)
        self.assertFalse(EarlyAccessSignup.objects.exists())
        codes = [self.anonymous.post("/api/contact/early-access/", {"email": f"p{i}@example.org"}, format="json").status_code for i in range(11)]
        self.assertEqual(codes[-1], 429)

    def test_mark_all_read(self):
        self.submit()
        self.anonymous.post("/api/contact/early-access/", {"email": "a@example.org"}, format="json")
        summary = self.client_admin.get("/api/contact/admin/summary/").json()
        self.assertEqual((summary["contact_unread"], summary["early_access_unread"]), (1, 1))
        done = self.client_admin.post("/api/contact/admin/mark-all-read/", {"kind": "contact"}, format="json").json()
        self.assertEqual(done["marked_read"], {"contact": 1})
        summary = self.client_admin.get("/api/contact/admin/summary/").json()
        self.assertEqual((summary["contact_unread"], summary["early_access_unread"]), (0, 1))
        self.client_admin.post("/api/contact/admin/mark-all-read/", {}, format="json")
        self.assertEqual(self.client_admin.get("/api/contact/admin/summary/").json()["early_access_unread"], 0)

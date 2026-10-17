"""The development email backend prints readable text and copyable links, and only while DEBUG is on."""
import io
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.test import SimpleTestCase, TestCase, override_settings

from accounts import invitations
from accounts.services import (build_verification_link, send_password_reset_email,
                               send_signup_verification_link)
from organizations.services import _send_invitation_email

from .dev_email import label_for, links_in

User = get_user_model()
DEV = "config.dev_email.DevConsoleEmailBackend"
FRONTEND = "http://localhost:5173"


def output_of(action, backend=DEV, debug=True):
    stream = io.StringIO()
    with override_settings(EMAIL_BACKEND=backend, DEBUG=debug, FRONTEND_URL=FRONTEND), redirect_stdout(stream):
        action()
    return stream.getvalue()


class LinkHelperTests(SimpleTestCase):
    def test_links_are_found_once_without_trailing_punctuation_or_html_escaping(self):
        text = 'Open http://a.test/x?y=1&amp;z=2. Again: http://a.test/x?y=1&z=2) and <a href="http://b.test/p">go</a>'
        self.assertEqual(links_in(text), ["http://a.test/x?y=1&z=2", "http://b.test/p"])

    def test_labels(self):
        self.assertEqual(label_for(f"{FRONTEND}/verify-email?registration_id=1&token=2"), "verify link")
        self.assertEqual(label_for(f"{FRONTEND}/reset-password/Mg/abc-def"), "password reset link")
        self.assertEqual(label_for(f"{FRONTEND}/invitations/abc"), "invitation link")
        self.assertEqual(label_for(f"{FRONTEND}/accept-invitation?token=abc"), "invitation link")
        self.assertEqual(label_for("http://x.test/other"), "link")


class DevEmailOutputTests(TestCase):
    def setUp(self):
        self.registration_id = uuid.uuid4()
        self.token = "tok_en-" + "x" * 60 + "~._"

    def test_the_console_backend_really_mangles_the_link(self):
        """Why this exists: the same message through Django's console backend is quoted-printable."""
        text = output_of(
            lambda: send_signup_verification_link("ada@example.com", self.registration_id, self.token, "Ada"),
            backend="django.core.mail.backends.console.EmailBackend",
        )
        self.assertIn("=3D", text)
        self.assertNotIn(build_verification_link(self.registration_id, self.token), text)

    def test_the_signup_link_is_printed_exactly_as_the_frontend_needs_it(self):
        text = output_of(lambda: send_signup_verification_link("ada@example.com", self.registration_id, self.token, "Ada Lovelace"))
        link = build_verification_link(self.registration_id, self.token)
        self.assertEqual(text.count(f"[dev email] verify link: {link}\n"), 1)
        self.assertIn(f"{FRONTEND}/verify-email?registration_id={self.registration_id}&token=", link)
        self.assertNotIn("=3D", text)
        self.assertNotRegex(text, r"(?m)[^=\n]=$")  # no quoted-printable soft line break at the end of a line
        self.assertIn("[dev email] To: ada@example.com", text)
        self.assertIn("Confirm your email address", text)

    def test_the_password_reset_link(self):
        user = User.objects.create_user(username="reset", email="reset@example.com", password="ForestRiver!4821", is_email_verified=True)
        text = output_of(lambda: send_password_reset_email(user))
        lines = [line for line in text.splitlines() if line.startswith("[dev email] password reset link: ")]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], rf"^\[dev email\] password reset link: {FRONTEND}/reset-password/[^/\s]+/[^/\s]+$")

    def test_account_and_organization_invitation_links(self):
        account = SimpleNamespace(role="moderator", email="inv@example.com")
        organization = SimpleNamespace(
            organization=SimpleNamespace(name="Acme"), role="member", email="inv2@example.com",
            get_role_display=lambda: "Member",
        )
        first = output_of(lambda: invitations._send(account, "tok123"))
        second = output_of(lambda: _send_invitation_email(organization, "tok456"))
        self.assertIn(f"[dev email] invitation link: {FRONTEND}/accept-invitation?token=tok123\n", first)
        self.assertIn(f"[dev email] invitation link: {FRONTEND}/invitations/tok456\n", second)

    def test_a_link_only_in_the_html_part_is_still_found(self):
        def send():
            message = EmailMultiAlternatives("Hi", "no link here", "from@example.com", ["to@example.com"])
            message.attach_alternative('<a href="http://app.test/verify-email?registration_id=1&amp;token=2">go</a>', "text/html")
            message.send()
        self.assertIn("[dev email] verify link: http://app.test/verify-email?registration_id=1&token=2\n", output_of(send))

    def test_nothing_is_marked_or_logged_when_debug_is_off(self):
        text = output_of(
            lambda: send_signup_verification_link("ada@example.com", self.registration_id, self.token, "Ada"), debug=False,
        )
        # Outside DEBUG it is exactly Django's console backend: raw MIME, no marked lines, no decoded link added.
        self.assertNotIn("[dev email]", text)
        self.assertTrue(text.startswith("Content-Type:") or text.startswith("MIME-Version:"))

    def test_real_delivery_is_unchanged(self):
        """The test runner's own backend still receives the original message, text and HTML, untouched."""
        send_signup_verification_link("ada@example.com", self.registration_id, self.token, "Ada")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn(build_verification_link(self.registration_id, self.token), message.body)
        self.assertEqual(message.alternatives[0][1], "text/html")
        self.assertEqual(message.to, ["ada@example.com"])

    def test_the_backend_is_a_console_backend_subclass(self):
        from .dev_email import DevConsoleEmailBackend
        self.assertTrue(issubclass(DevConsoleEmailBackend, ConsoleBackend))

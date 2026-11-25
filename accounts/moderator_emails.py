"""Emails that tell a person what happened to their account's role (never a password).

* ``send_account_ready``: after an invitation was accepted: "your account is ready", with the e-mail address and username to sign in with.
* ``send_account_created_by_admin``: an administrator created the account: the sign-in details and a link to choose a password.
* ``send_made_moderator``: an existing account became a moderator of an organization.

A password is never put in an e-mail (see ``create_account_as_admin``: it appears in no event, log line or response). Where the person has to choose one, the e-mail carries the
same one-time link as "forgot password". Each message is sent after the surrounding transaction commits and a delivery problem is logged, never raised: the administrator's action
(a role change, an account, an accepted invitation) must not fail because a mail server was unreachable.
"""
import logging

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

logger = logging.getLogger(__name__)


def _site():
    return settings.FRONTEND_URL.rstrip("/")


def _who(user, organization, role=None):
    lines = [f"Email: {user.email}", f"Username: {user.get_username()}", f"Role: {role or user.role}"]
    if organization is not None:
        lines.append(f"Organization: {organization.name}")
    return "\n".join(lines)


def _deliver(user, subject, body):
    try:
        send_mail(subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[user.email], fail_silently=False)
    except Exception:  # noqa: BLE001 - a mail problem must never undo the action that caused the mail
        logger.exception("Could not send the %r e-mail to user %s.", subject, user.pk)
        return False
    return True


def _after_commit(user, subject, build_body):
    transaction.on_commit(lambda: _deliver(user, subject, build_body()))


def send_account_ready(user, organization=None):
    def body():
        return (
            "Your FaceCloud account is ready.\n\n"
            f"{_who(user, organization)}\n\n"
            f"Sign in: {_site()}/login\n\n"
            "Your password is the one you chose when you accepted the invitation. It is never sent by e-mail. "
            "If you have forgotten it, use \"Forgot password\" on the sign-in page."
        )

    _after_commit(user, "Your FaceCloud account is ready", body)


def send_account_created_by_admin(user, organization=None):
    def body():
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return (
            "An administrator created a FaceCloud account for you.\n\n"
            f"{_who(user, organization)}\n\n"
            f"Choose your own password here: {_site()}/reset-password/{uid}/{token}\n"
            f"Then sign in: {_site()}/login\n\n"
            "This e-mail never contains a password. If your administrator already gave you one, you can sign in with it and change it under Settings."
        )

    _after_commit(user, "Your FaceCloud account was created", body)


def send_made_moderator(user, organization):
    def body():
        place = f" for {organization.name}" if organization is not None else ""
        return (
            f"You are now a moderator{place} on FaceCloud.\n\n"
            f"{_who(user, organization, role='moderator')}\n\n"
            f"Open your moderator panel: {_site()}/moderator\n"
            f"(Sign in first at {_site()}/login if you are signed out.)\n\n"
            "If you were not expecting this, contact the administrator."
        )

    _after_commit(user, "You are now a moderator on FaceCloud", body)

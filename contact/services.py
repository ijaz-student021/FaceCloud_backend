import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ContactMessage, EarlyAccessSignup

logger = logging.getLogger(__name__)


def contact_recipients():
    return [address for address in settings.CONTACT_RECIPIENT_EMAILS if address]


def purge_old_messages(now=None):
    cutoff = (now or timezone.now()) - timedelta(days=settings.CONTACT_MESSAGE_RETENTION_DAYS)
    return ContactMessage.objects.filter(created_at__lt=cutoff).delete()[0]


def submit_message(**fields):
    """Store the message, tell the administrators, and queue its e-mail once the row is committed (no recipient set: stored and shown in the admin inbox only)."""
    from .tasks import deliver_contact_message

    purge_old_messages()
    message = ContactMessage.objects.create(**fields)
    if contact_recipients():
        transaction.on_commit(lambda: deliver_contact_message.delay(message.pk))
    else:
        ContactMessage.objects.filter(pk=message.pk).update(delivery_status=ContactMessage.Delivery.SKIPPED)
    transaction.on_commit(lambda: _tell_admins("contact", message))
    return message


def submit_early_access(email):
    """Store an early-access address (once per address) and tell the administrators. Returns ``(signup, created)``."""
    signup, created = _get_or_create_signup(email)
    if created:
        transaction.on_commit(lambda: _tell_admins("early_access", signup))
    return signup, created


def _get_or_create_signup(email):
    existing = EarlyAccessSignup.objects.filter(email__iexact=email).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            return EarlyAccessSignup.objects.create(email=email), True
    except IntegrityError:          # the same address arrived at the same moment
        return EarlyAccessSignup.objects.get(email__iexact=email), False


def _tell_admins(kind, row):
    """The in-app notification for every administrator; never raises (a notification problem must not lose or fail the submission)."""
    from notifications.events import notify_admins_contact_message, notify_admins_early_access

    try:
        (notify_admins_contact_message if kind == "contact" else notify_admins_early_access)(row)
    except Exception:
        logger.exception("Could not notify the administrators of %s %s.", kind, row.pk)


def deliver(message):
    """Send one message to the team; the submitter is the Reply-To address."""
    body = (
        f"From: {message.name} <{message.email}>\n"
        f"Company: {message.company or '-'}\n"
        f"Reason: {message.reason}\n"
        f"Received: {message.created_at.isoformat()}\n\n"
        f"{message.message}\n"
    )
    EmailMessage(
        subject=f"[FaceCloud contact] {message.reason}: {message.name}"[:200],
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=contact_recipients(),
        reply_to=[message.email],
    ).send(fail_silently=False)

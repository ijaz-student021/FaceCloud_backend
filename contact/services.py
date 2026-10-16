from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from .models import ContactMessage


def contact_recipients():
    return [address for address in settings.CONTACT_RECIPIENT_EMAILS if address]


def purge_old_messages(now=None):
    cutoff = (now or timezone.now()) - timedelta(days=settings.CONTACT_MESSAGE_RETENTION_DAYS)
    return ContactMessage.objects.filter(created_at__lt=cutoff).delete()[0]


def submit_message(**fields):
    """Store the message and queue its delivery once the row is committed."""
    from .tasks import deliver_contact_message

    purge_old_messages()
    message = ContactMessage.objects.create(**fields)
    transaction.on_commit(lambda: deliver_contact_message.delay(message.pk))
    return message


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

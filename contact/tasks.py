import logging
import smtplib

from celery import shared_task
from django.utils import timezone

from .models import ContactMessage
from .services import deliver

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 5


@shared_task(bind=True, name="contact.deliver", max_retries=MAX_DELIVERY_ATTEMPTS - 1)
def deliver_contact_message(self, message_id):
    message = ContactMessage.objects.filter(pk=message_id).first()
    if message is None or message.delivery_status == ContactMessage.Delivery.SENT:
        return
    ContactMessage.objects.filter(pk=message_id).update(delivery_attempts=message.delivery_attempts + 1)
    try:
        deliver(message)
    except (smtplib.SMTPException, OSError) as error:
        if self.request.retries < self.max_retries and not self.request.is_eager:
            raise self.retry(exc=error, countdown=min(3600, 60 * 2 ** self.request.retries))
        logger.exception("Contact message %s could not be delivered.", message_id)
        ContactMessage.objects.filter(pk=message_id).update(
            delivery_status=ContactMessage.Delivery.FAILED, failure_reason=str(error)[:255],
        )
        return
    ContactMessage.objects.filter(pk=message_id).update(
        delivery_status=ContactMessage.Delivery.SENT, delivered_at=timezone.now(), failure_reason="",
    )

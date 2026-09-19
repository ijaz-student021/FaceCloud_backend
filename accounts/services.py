from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.throttling import BaseThrottle

from .models import AuthEvent


def get_client_ip(request):
    """Client address as seen by the rate limiter.

    X-Forwarded-For is client-controlled, so it is only honoured for the
    number of trusted proxies in REST_FRAMEWORK["NUM_PROXIES"]; otherwise the
    audit log and the throttles would both be spoofable with one header.
    """
    return BaseThrottle().get_ident(request)


def record_auth_event(event, request=None, user=None, identifier="", details=None):
    return AuthEvent.objects.create(
        user=user,
        event=event,
        identifier=identifier[:254],
        ip_address=get_client_ip(request) if request else None,
        details=details or {},
    )


def send_verification_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse("verify-email", kwargs={"uidb64": uid, "token": token})
    link = urljoin(settings.PUBLIC_BACKEND_URL.rstrip("/") + "/", path.lstrip("/"))
    send_mail(
        subject="Verify your FaceCloud account",
        message=f"Click the link to verify your email: {link}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def send_password_reset_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password/{uid}/{token}"
    send_mail(
        subject="Reset your FaceCloud password",
        message=f"Use this link to reset your password: {link}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )

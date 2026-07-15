from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import AuthEvent


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR")


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

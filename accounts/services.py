import secrets
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives, send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.html import escape
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
    path = reverse(
        "verify-email-change",
        kwargs={"uidb64": uid, "token": token},
    )
    link = urljoin(settings.PUBLIC_BACKEND_URL.rstrip("/") + "/", path.lstrip("/"))
    send_mail(
        subject="Verify your FaceCloud account",
        message=f"Click the link to verify your email: {link}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def generate_verification_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def mask_email(email):
    local_part, separator, domain = email.partition("@")
    if not separator:
        return "***"
    visible = local_part[:1]
    return f"{visible}{'*' * max(2, len(local_part) - 1)}@{domain}"


def send_signup_verification_code(email, code):
    lifetime_minutes = settings.SIGNUP_VERIFICATION_CODE_LIFETIME_SECONDS // 60
    support_email = settings.SIGNUP_VERIFICATION_SUPPORT_EMAIL
    message = (
        "Welcome to FaceCloud.\n\n"
        f"Your verification code is:\n\n{code}\n\n"
        f"This code expires in {lifetime_minutes} minutes. "
        "Do not share it with anyone.\n\n"
        "If you did not request a FaceCloud account, you can ignore this email.\n"
        f"Support: {support_email}"
    )
    html_message = (
        '<div style="font-family:Arial,sans-serif;line-height:1.5;color:#111827">'
        "<h1>Welcome to FaceCloud</h1>"
        "<p>Your verification code is:</p>"
        '<p style="font-size:32px;font-weight:700;letter-spacing:8px">'
        f"{escape(code)}</p>"
        f"<p>This code expires in {lifetime_minutes} minutes. "
        "Do not share it with anyone.</p>"
        "<p>If you did not request a FaceCloud account, you can ignore this email.</p>"
        f"<p>Support: {escape(support_email)}</p>"
        "</div>"
    )
    email_message = EmailMultiAlternatives(
        subject="Your FaceCloud verification code",
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    email_message.attach_alternative(html_message, "text/html")
    email_message.send(fail_silently=False)


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

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode, urljoin

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.html import escape
from django.utils.http import urlsafe_base64_encode
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.throttling import BaseThrottle

from . import lockout, totp
from .models import AuthEvent, TwoFactorCredential, TwoFactorRecoveryCode
from .two_factor_crypto import TwoFactorSecretError, decrypt_secret, encrypt_secret


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


def generate_verification_token():
    """A single-use link token: 256 bits, URL-safe. Only its SHA-256 is stored (see ``hash_verification_token``)."""
    return secrets.token_urlsafe(32)


def hash_verification_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verification_token_matches(token, stored_hash):
    """Constant-time comparison; an empty stored hash (a consumed or invalidated registration) never matches."""
    if not stored_hash or not token:
        return False
    return hmac.compare_digest(hash_verification_token(token), stored_hash)


def mask_email(email):
    local_part, separator, domain = email.partition("@")
    if not separator:
        return "***"
    visible = local_part[:1]
    return f"{visible}{'*' * max(2, len(local_part) - 1)}@{domain}"


def build_verification_link(registration_id, token):
    query = urlencode({"registration_id": str(registration_id), "token": token})
    return f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?{query}"


def send_signup_verification_link(email, registration_id, token, full_name=""):
    """Email the signup link. The token appears only in this message: it is never logged or stored."""
    link = build_verification_link(registration_id, token)
    lifetime_hours = max(1, settings.SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS // 3600)
    support_email = settings.SIGNUP_VERIFICATION_SUPPORT_EMAIL
    greeting = f"Hi {full_name.split()[0]}," if full_name.strip() else "Welcome to FaceCloud."
    message = (
        f"{greeting}\n\n"
        "Confirm your email address to finish creating your FaceCloud account:\n\n"
        f"{link}\n\n"
        f"This link works once and expires in {lifetime_hours} hours. Do not share it with anyone.\n\n"
        "If you did not request a FaceCloud account, you can ignore this email.\n"
        f"Support: {support_email}"
    )
    html_message = (
        '<div style="font-family:Arial,sans-serif;line-height:1.5;color:#111827">'
        "<h1>Welcome to FaceCloud</h1>"
        f"<p>{escape(greeting)}</p>"
        "<p>Confirm your email address to finish creating your account:</p>"
        f'<p><a href="{escape(link)}" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#ffffff;'
        'text-decoration:none;border-radius:6px;font-weight:700">Verify email</a></p>'
        f'<p>Or paste this link into your browser:<br><a href="{escape(link)}">{escape(link)}</a></p>'
        f"<p>This link works once and expires in {lifetime_hours} hours. Do not share it with anyone.</p>"
        "<p>If you did not request a FaceCloud account, you can ignore this email.</p>"
        f"<p>Support: {escape(support_email)}</p>"
        "</div>"
    )
    email_message = EmailMultiAlternatives(
        subject="Verify your FaceCloud email address",
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


# -- Two-factor authentication (TOTP) ----------------------------------------

TWO_FACTOR_CHALLENGE_SALT = "accounts.two_factor.login_challenge"


_USERNAME_DISALLOWED = re.compile(r"[^A-Za-z0-9@.+\-_]")
USERNAME_MAX_LENGTH = 150


def split_full_name(full_name):
    """``"  Ada   King Lovelace "`` -> ``("Ada", "King Lovelace")``: first word, then the rest (both within Django's 150)."""
    first, _separator, rest = " ".join((full_name or "").split()).partition(" ")
    return first[:150], rest[:150]


def derive_username(email):
    """A unique username from the email's local part: only characters Django allows, at most 150, numeric suffix until free.

    Unique, case-insensitively, against existing users and against *other people's* active pending registrations; a pending
    registration for this same email does not count, so registering twice with one address keeps one pending row.
    """
    from .models import PendingRegistration

    base = _USERNAME_DISALLOWED.sub("", email.partition("@")[0])[:USERNAME_MAX_LENGTH] or "user"

    def taken(name):
        return (
            get_user_model().objects.filter(username__iexact=name).exists()
            or PendingRegistration.objects.filter(
                username__iexact=name, status__in=PendingRegistration.ACTIVE_STATUSES,
            ).exclude(email__iexact=email).exists()
        )

    candidate, number = base, 1
    while taken(candidate):
        number += 1
        suffix = str(number)
        candidate = base[: USERNAME_MAX_LENGTH - len(suffix)] + suffix
    return candidate


def dashboard_for_user(user):
    if user.is_superuser or user.role == "admin":
        return "admin"
    if user.role == "moderator":
        return "moderator"
    return "user"


def issue_login_tokens(user):
    """The same ``{refresh, access, user, role, dashboard}`` shape every login path returns."""
    from rest_framework_simplejwt.tokens import RefreshToken

    from .serializers import UserSerializer  # deferred: serializers.py imports this module

    token = RefreshToken.for_user(user)
    token["role"] = user.role
    token["token_version"] = user.token_version
    return {
        "refresh": str(token),
        "access": str(token.access_token),
        "user": UserSerializer(user).data,
        "role": user.role,
        "dashboard": dashboard_for_user(user),
    }


def has_active_two_factor(user):
    return TwoFactorCredential.objects.filter(user=user, confirmed_at__isnull=False).exists()


def make_two_factor_login_challenge(user):
    """A short-lived signed marker that this user's password has just been verified.

    Not the TOTP secret and not a real access token -- only ever exchanged, within
    ``TWO_FACTOR_LOGIN_CHALLENGE_MINUTES``, for real tokens once the second factor
    is also verified.
    """
    return signing.TimestampSigner(salt=TWO_FACTOR_CHALLENGE_SALT).sign(str(user.pk))


def _consume_two_factor_login_challenge(token):
    try:
        value = signing.TimestampSigner(salt=TWO_FACTOR_CHALLENGE_SALT).unsign(
            token, max_age=settings.TWO_FACTOR_LOGIN_CHALLENGE_MINUTES * 60,
        )
    except signing.BadSignature:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def generate_recovery_codes(credential):
    """Replace ``credential``'s recovery codes with a fresh set; returns the plaintext codes (shown once, never stored)."""
    credential.recovery_codes.all().delete()
    codes = [secrets.token_hex(5) for _ in range(settings.TWO_FACTOR_RECOVERY_CODE_COUNT)]
    TwoFactorRecoveryCode.objects.bulk_create([
        TwoFactorRecoveryCode(credential=credential, code_hash=TwoFactorRecoveryCode.hash_code(code))
        for code in codes
    ])
    return codes


def setup_two_factor(user):
    """Start (or restart) enrollment: a fresh secret, not yet enforced at login.

    Refuses while a confirmed credential already exists -- disable it first (which
    itself requires proof of the current device or a recovery code), so a bare
    authenticated session alone can never silently replace an active enrollment.
    """
    if has_active_two_factor(user):
        raise ValidationError({"detail": "Two-factor authentication is already enabled. Disable it first to reset it."})
    secret = totp.generate_secret()
    TwoFactorCredential.objects.update_or_create(
        user=user,
        defaults={"encrypted_secret": encrypt_secret(secret), "confirmed_at": None, "last_used_counter": None},
    )
    return {
        "secret": secret,
        "otpauth_uri": totp.provisioning_uri(secret, user.email or user.username, settings.TWO_FACTOR_ISSUER),
    }


@transaction.atomic
def confirm_two_factor(user, code, request=None):
    """Turn on enrollment by proving one real code from the device; returns the recovery codes."""
    credential = TwoFactorCredential.objects.select_for_update().filter(user=user, confirmed_at__isnull=True).first()
    if credential is None:
        raise ValidationError({"detail": "Start two-factor setup before confirming it."})
    try:
        secret = decrypt_secret(credential.encrypted_secret)
    except TwoFactorSecretError as error:
        raise ValidationError({"detail": str(error)}) from error
    valid, counter = totp.verify_code(secret, code)
    if not valid:
        raise ValidationError({"code": "That code is not valid. Check the time on your device and try again."})
    credential.confirmed_at = timezone.now()
    credential.last_used_counter = counter
    credential.save(update_fields=["confirmed_at", "last_used_counter", "updated_at"])
    codes = generate_recovery_codes(credential)
    record_auth_event("two_factor_enabled", request, user)
    return codes


def two_factor_status(user):
    return {"enabled": has_active_two_factor(user)}


def _verify_second_factor(credential, code):
    """Try ``code`` as a TOTP code, then as a recovery code. ``(ok, updated_counter_or_None)``.

    A recovery-code success returns ``counter=None``: it does not move the replay
    watermark, since it is not a TOTP step.
    """
    try:
        secret = decrypt_secret(credential.encrypted_secret)
    except TwoFactorSecretError:
        secret = None
    if secret is not None:
        valid, counter = totp.verify_code(secret, code, last_used_counter=credential.last_used_counter)
        if valid:
            return True, counter
    for recovery_code in credential.recovery_codes.filter(used_at__isnull=True):
        if recovery_code.matches(code):
            recovery_code.used_at = timezone.now()
            recovery_code.save(update_fields=["used_at"])
            return True, None
    return False, None


def _require_active_credential_with_lockout(user):
    credential = TwoFactorCredential.objects.select_for_update().filter(user=user, confirmed_at__isnull=False).first()
    if credential is None:
        raise ValidationError({"detail": "Two-factor authentication is not enabled."})
    key = lockout.attempt_key(user, "")
    remaining = lockout.seconds_locked(key)
    if remaining:
        raise Throttled(wait=remaining, detail=f"Too many failed attempts. Try again in {remaining} seconds.")
    return credential, key


def disable_two_factor(user, code, request=None):
    """Turn off 2FA. Requires a current TOTP code or an unused recovery code --
    never just an authenticated session -- so a stolen access token alone cannot
    remove this protection."""
    with transaction.atomic():
        credential, key = _require_active_credential_with_lockout(user)
        ok, _counter = _verify_second_factor(credential, code)
        if ok:
            lockout.clear(key)
            credential.delete()
    if not ok:
        # Recorded after the transaction above, not inside it: raising from within
        # an atomic block rolls back everything in it, including a failure count
        # that must actually persist for the lockout to work.
        lockout.record_failure(key)
        raise ValidationError({"code": "That code is not valid."})
    record_auth_event("two_factor_disabled", request, user)


def regenerate_recovery_codes(user, code, request=None):
    """Invalidate all existing recovery codes and issue a fresh set, proven by a live code."""
    with transaction.atomic():
        credential, key = _require_active_credential_with_lockout(user)
        ok, counter = _verify_second_factor(credential, code)
        if ok:
            lockout.clear(key)
            if counter is not None:
                credential.last_used_counter = counter
                credential.save(update_fields=["last_used_counter", "updated_at"])
            codes = generate_recovery_codes(credential)
    if not ok:
        lockout.record_failure(key)
        raise ValidationError({"code": "That code is not valid."})
    record_auth_event("two_factor_recovery_codes_regenerated", request, user)
    return codes


def verify_two_factor_login(login_token, code, request=None):
    """The second step of login: exchange a password-verified challenge plus a
    device code (or recovery code) for real tokens.

    Re-checks every account-state rule the password step already checked (active,
    verified, not suspended, 2FA still enabled) rather than trusting that earlier
    result -- the account can change state in the few minutes between the two steps.
    """
    user_id = _consume_two_factor_login_challenge(login_token)
    user = get_user_model().objects.filter(pk=user_id).first() if user_id is not None else None
    if user is None:
        raise ValidationError({"detail": "This login has expired. Sign in again."})
    if not user.is_active or not user.is_email_verified or user.is_suspended:
        record_auth_event("login_failed", request, user, "", {"reason": "account_state_changed", "stage": "two_factor"})
        raise ValidationError({"detail": "This account cannot sign in right now."})
    with transaction.atomic():
        credential, key = _require_active_credential_with_lockout(user)
        ok, counter = _verify_second_factor(credential, code)
        if ok:
            lockout.clear(key)
            if counter is not None:
                credential.last_used_counter = counter
                credential.save(update_fields=["last_used_counter", "updated_at"])
    if not ok:
        # Outside the transaction above, so this failure is never rolled back by
        # the ValidationError it is about to raise (see disable_two_factor).
        record_auth_event("login_failed", request, user, "", {"reason": "invalid_code", "stage": "two_factor"})
        delay = lockout.record_failure(key)
        if delay:
            record_auth_event("login_locked", request, user, "", {"seconds": delay})
        raise ValidationError({"code": "That code is not valid."})
    record_auth_event("login", request, user, "", {"stage": "two_factor"})
    return issue_login_tokens(user)

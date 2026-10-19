import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode, urljoin

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.html import escape
from django.utils.http import urlsafe_base64_encode
from rest_framework.exceptions import APIException, PermissionDenied
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


def purge_stale_registrations(now=None):
    """Delete abandoned signups and finished ones past ``SIGNUP_VERIFICATION_RETENTION_HOURS``; returns how many rows went.

    Abandoned: pending, locked, expired or cancelled, and the link expired more than the retention window ago. Finished: consumed
    (the account exists) and not touched for longer than the window. A recent or still-valid registration is never removed.
    """
    from datetime import timedelta

    from django.db.models import Q

    from .models import PendingRegistration

    cutoff = (now or timezone.now()) - timedelta(hours=settings.SIGNUP_VERIFICATION_RETENTION_HOURS)
    abandoned = Q(
        status__in=[
            PendingRegistration.STATUS_PENDING,
            PendingRegistration.STATUS_LOCKED,
            PendingRegistration.STATUS_EXPIRED,
            PendingRegistration.STATUS_CANCELLED,
        ],
        token_expires_at__lt=cutoff,
    )
    finished = Q(status=PendingRegistration.STATUS_CONSUMED, updated_at__lt=cutoff)
    deleted, _ = PendingRegistration.objects.filter(abandoned | finished).delete()
    return deleted


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


def moderator_memberships(user):
    """The person's active moderator or owner memberships in active, non-personal organizations (the places a moderator can work)."""
    from organizations.models import Organization, OrganizationMembership

    return OrganizationMembership.objects.filter(
        user=user, status=OrganizationMembership.Status.ACTIVE,
        role__in=(OrganizationMembership.Role.MODERATOR, OrganizationMembership.Role.OWNER),
        organization__status=Organization.Status.ACTIVE, organization__is_personal=False,
    )


def has_moderator_organization(user):
    return moderator_memberships(user).exists()


class ModeratorWithoutOrganization(PermissionDenied):
    """A moderator account that belongs to no organization may not sign in: a moderator works for exactly one, and sees only what is theirs."""

    default_code = "no_organization"

    def __init__(self):
        super().__init__(detail={"detail": "Your moderator account is not assigned to an organization yet.", "code": "no_organization"})


def dashboard_for_user(user):
    """The portal this person should land in, which is also the ``role`` the login response reports.

    ``admin``: a superuser or ``User.role == "admin"``. ``moderator``: an *active moderator membership* in an *active, non-personal*
    organization (a stored ``User.role == "moderator"`` with no such membership is not a moderator: it cannot sign in, see
    ``ModeratorWithoutOrganization``). Everyone else, including an organization's owner, is ``user``. This only chooses the portal;
    ``User.role`` and the permission classes that read it are unchanged.
    """
    if user.is_superuser or user.role == "admin":
        return "admin"
    from organizations.models import OrganizationMembership

    if moderator_memberships(user).filter(role=OrganizationMembership.Role.MODERATOR).exists():
        return "moderator"
    return "user"


def issue_login_tokens(user, request=None):
    """The same ``{refresh, access, user, role, dashboard}`` shape every login path returns.

    Each login starts a session (accounts/sessions.py) named in both tokens.
    """
    from rest_framework_simplejwt.tokens import RefreshToken

    from .serializers import UserSerializer  # deferred: serializers.py imports this module
    from .sessions import attach, start_session

    token = RefreshToken.for_user(user)
    token["role"] = user.role
    token["token_version"] = user.token_version
    attach(start_session(user, request), token)
    portal = dashboard_for_user(user)
    return {
        "refresh": str(token),
        "access": str(token.access_token),
        "user": UserSerializer(user).data,
        # The frontend keeps ``role`` as its session role and routes on ``dashboard``: they are the same value.
        "role": portal,
        "dashboard": portal,
    }


class OrganizationChoiceError(APIException):
    """A 400 whose body is a plain ``{"detail": "..."}`` message (a DRF ``ValidationError`` would wrap it in a list)."""

    status_code = 400
    default_code = "organization_choice"


def resolve_moderator_organization(organization_id=None):
    """The organization a new moderator joins: the one asked for, else the only active shared one.

    Raises ``ValidationError`` (a plain ``detail`` message) when it does not exist, is not active, is somebody's personal workspace,
    or when nothing or too many candidates leave the choice open.
    """
    from organizations.models import Organization

    shared = Organization.objects.filter(status=Organization.Status.ACTIVE, is_personal=False)
    if organization_id is not None:
        organization = Organization.objects.filter(pk=organization_id).first()
        if organization is None or organization.status != Organization.Status.ACTIVE:
            raise OrganizationChoiceError("That organization does not exist or is not active.")
        if organization.is_personal:
            raise OrganizationChoiceError("That is a personal workspace, not an organization a moderator can join.")
        return organization
    candidates = list(shared.order_by("name")[:2])
    if not candidates:
        raise OrganizationChoiceError("Create an organization first, then try again.")
    if len(candidates) > 1:
        raise OrganizationChoiceError("There is more than one organization: send organization_id to choose one.")
    return candidates[0]


@transaction.atomic
def create_account_as_admin(*, administrator, username, email, password, role, organization=None, full_name="", request=None):
    """Create an already-verified account for someone, in one transaction.

    ``role`` is ``user`` or ``moderator`` (never ``admin``: the serializer allows only those two). A moderator also gets an active
    moderator membership in ``organization`` so the moderator panel can find it; if that fails nothing is created. The password is
    hashed by ``create_user`` and appears in no event, log line or response. One ``admin_created_account`` event names the
    administrator and the new account.
    """
    from organizations.models import OrganizationMembership

    User = get_user_model()
    first_name, last_name = split_full_name(full_name)
    user = User.objects.create_user(
        username=username, email=email, password=password, first_name=first_name, last_name=last_name,
        role=role, is_email_verified=True, is_active=True, is_suspended=False,
    )
    if role == "moderator":
        OrganizationMembership.objects.create(
            organization=organization, user=user, role=OrganizationMembership.Role.MODERATOR,
            status=OrganizationMembership.Status.ACTIVE, invited_by=administrator,
        )
    record_auth_event(
        "admin_created_account", request, user, email,
        {"administrator_id": administrator.pk, "administrator": administrator.get_username(), "role": role,
         "organization_id": organization.pk if organization is not None else None},
    )
    return user

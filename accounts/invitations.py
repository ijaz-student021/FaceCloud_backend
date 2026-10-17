"""Administrator invitations: create an account by verified email invitation.

Role granting is enforced here and by the admin role update: an administrator
may grant "user" or "moderator"; only a superuser may grant "admin".
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from .models import AccountInvitation, User


class InvitationConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "invitation_conflict"


def grantable_roles(actor):
    if actor.is_superuser:
        return {"user", "moderator", "admin"}
    if actor.role == "admin":
        return {"user", "moderator"}
    return set()


def assert_can_grant(actor, role):
    if role not in grantable_roles(actor):
        raise PermissionDenied(f"You may not grant the {role} role.")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _send(invitation, token):
    link = f"{settings.FRONTEND_URL.rstrip('/')}/accept-invitation?token={token}"
    send_mail(
        subject="You've been invited to FaceCloud",
        message=(
            f"You've been invited to create a FaceCloud account with the {invitation.role} role.\n\n"
            f"Create your account: {link}\n\n"
            f"This invitation expires in {settings.ACCOUNT_INVITATION_TTL_HOURS} hours. "
            "If you weren't expecting it, you can ignore this email."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[invitation.email], fail_silently=False,
    )


def _expire_stale():
    AccountInvitation.objects.filter(status=AccountInvitation.Status.PENDING, expires_at__lte=timezone.now()).update(
        status=AccountInvitation.Status.EXPIRED)


def invite(*, actor, email, role):
    assert_can_grant(actor, role)
    email = email.strip().lower()
    if User.objects.annotate(lower_email=Lower("email")).filter(lower_email=email).exists():
        raise InvitationConflict("An account with this email already exists.")
    _expire_stale()
    token = secrets.token_urlsafe(32)
    try:
        with transaction.atomic():
            invitation = AccountInvitation.objects.create(
                email=email, role=role, token_hash=_hash(token), invited_by=actor,
                expires_at=timezone.now() + timedelta(hours=settings.ACCOUNT_INVITATION_TTL_HOURS),
            )
    except IntegrityError as error:
        raise InvitationConflict("A pending invitation for this email already exists.") from error
    transaction.on_commit(lambda: _send(invitation, token))
    return invitation


def resend(invitation, actor):
    assert_can_grant(actor, invitation.role)
    if invitation.status not in (AccountInvitation.Status.PENDING, AccountInvitation.Status.EXPIRED):
        raise InvitationConflict(f"This invitation is {invitation.status}.")
    token = secrets.token_urlsafe(32)
    invitation.token_hash = _hash(token)
    invitation.status = AccountInvitation.Status.PENDING
    invitation.expires_at = timezone.now() + timedelta(hours=settings.ACCOUNT_INVITATION_TTL_HOURS)
    try:
        invitation.save(update_fields=["token_hash", "status", "expires_at", "updated_at"])
    except IntegrityError as error:
        raise InvitationConflict("Another pending invitation for this email exists.") from error
    transaction.on_commit(lambda: _send(invitation, token))
    return invitation


def cancel(invitation, actor):
    assert_can_grant(actor, invitation.role)
    if invitation.status != AccountInvitation.Status.PENDING:
        raise InvitationConflict(f"This invitation is {invitation.status}.")
    invitation.status = AccountInvitation.Status.CANCELLED
    invitation.save(update_fields=["status", "updated_at"])
    return invitation


def pending_invitation(token):
    """The pending, unexpired invitation for ``token``, or None."""
    _expire_stale()
    return AccountInvitation.objects.filter(token_hash=_hash(token or ""),
                                            status=AccountInvitation.Status.PENDING).first()


def accept(*, token, username, password):
    """Create the invited account (email verified, invited role). Raises on any problem."""
    from organizations.services import create_personal_organization

    with transaction.atomic():
        invitation = AccountInvitation.objects.select_for_update().filter(
            token_hash=_hash(token or ""), status=AccountInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).first()
        if invitation is None:
            raise ValidationError({"token": "This invitation is invalid, used or expired."})
        username = User.normalize_username(username).strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError({"username": "A user with this username already exists."})
        if User.objects.annotate(lower_email=Lower("email")).filter(lower_email=invitation.email).exists():
            raise InvitationConflict("An account with this email already exists.")
        candidate = User(username=username, email=invitation.email)
        try:
            password_validation.validate_password(password, user=candidate)
        except Exception as error:  # django.core.exceptions.ValidationError
            raise ValidationError({"password": list(getattr(error, "messages", [str(error)]))}) from error
        user = User.objects.create_user(username=username, email=invitation.email, password=password,
                                        role=invitation.role, is_email_verified=True)
        create_personal_organization(user)
        invitation.status = AccountInvitation.Status.ACCEPTED
        invitation.accepted_user = user
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["status", "accepted_user", "accepted_at", "updated_at"])
    return user, invitation

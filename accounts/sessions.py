"""Signed-in sessions: one per login, named in every token as ``sid``.

* Login (password, or password + second factor) starts a session.
* Refresh requires the session to be active and moves it to the new refresh
  token (rotation).
* Authentication refuses an access token whose session has ended, so revoking a
  session takes effect at once, not when its 15-minute access token expires.
* Revoking blacklists the session's current refresh token as well.

Tokens issued before sessions existed carry no ``sid``; they stay valid until
they expire (at most the refresh lifetime) and are not listed.
"""
import re
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import UserSession

SID_CLAIM = "sid"


def _refresh_lifetime():
    return settings.SIMPLE_JWT.get("REFRESH_TOKEN_LIFETIME", timedelta(days=7))


def start_session(user, request=None):
    from .services import get_client_ip

    now = timezone.now()
    agent = (request.META.get("HTTP_USER_AGENT", "") if request is not None else "")[:256]
    return UserSession.objects.create(
        user=user,
        token_version=user.token_version,
        user_agent=agent,
        ip_address=get_client_ip(request) if request is not None else None,
        last_seen_at=now,
        expires_at=now + _refresh_lifetime(),
    )


def attach(session, refresh_token):
    """Name the session in a newly issued refresh token (and its access tokens)."""
    refresh_token[SID_CLAIM] = str(session.pk)
    session.refresh_jti = refresh_token["jti"]
    session.save(update_fields=["refresh_jti"])


def session_for_token(token):
    sid = token.get(SID_CLAIM)
    if not sid:
        return None
    return UserSession.objects.filter(pk=sid).first()


def token_session_is_active(token, user):
    """True for an active session of ``user``, or a legacy token with no session."""
    if not token.get(SID_CLAIM):
        return True
    session = session_for_token(token)
    return session is not None and session.user_id == user.pk and session.is_active_for(user, timezone.now())


def record_rotation(new_refresh_token):
    session = session_for_token(new_refresh_token)
    if session is None:
        return
    now = timezone.now()
    UserSession.objects.filter(pk=session.pk).update(
        refresh_jti=new_refresh_token["jti"], last_seen_at=now, expires_at=now + _refresh_lifetime(),
    )


def revoke(session, reason):
    """End a session and blacklist its current refresh token."""
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    if session.revoked_at is None:
        session.revoked_at = timezone.now()
        session.revoked_reason = reason[:32]
        session.save(update_fields=["revoked_at", "revoked_reason"])
    outstanding = OutstandingToken.objects.filter(jti=session.refresh_jti).first() if session.refresh_jti else None
    if outstanding is not None:
        BlacklistedToken.objects.get_or_create(token=outstanding)


def revoke_all(user, reason):
    for session in UserSession.objects.filter(user=user, revoked_at__isnull=True):
        revoke(session, reason)


def active_sessions(user):
    """The user's active sessions, newest activity first; old records are purged."""
    now = timezone.now()
    cutoff = now - timedelta(days=settings.SESSION_RECORD_RETENTION_DAYS)
    UserSession.objects.filter(user=user).filter(
        Q(revoked_at__lt=cutoff) | Q(expires_at__lt=cutoff)
        | (Q(last_seen_at__lt=cutoff) & ~Q(token_version=user.token_version)),
    ).delete()
    return [
        session for session in UserSession.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=now)
        if session.token_version == user.token_version
    ]


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("iPhone", "iOS"), ("iPad", "iPadOS"), ("Android", "Android"), ("Mac OS X", "macOS"),
            ("Windows", "Windows"), ("Linux", "Linux"))


def describe_device(user_agent):
    """A short label such as "Chrome on macOS"; "Unknown device" when unclear."""
    browser = next((name for marker, name in _BROWSERS if marker in user_agent), None)
    system = next((name for marker, name in _SYSTEMS if marker in user_agent), None)
    if not browser and re.search(r"python|curl|okhttp|httpie", user_agent, re.I):
        browser = "API client"
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"

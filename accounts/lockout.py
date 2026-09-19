"""Per-account login backoff.

The per-address throttle limits one client; it does nothing against a password
guesser spread across many addresses. This slows guessing against a single
account instead: after LOGIN_LOCK_THRESHOLD consecutive failures the account is
locked for LOGIN_LOCK_BASE_SECONDS, doubling with every further failure up to
LOGIN_LOCK_MAX_SECONDS. While locked, even the correct password is refused.

Design points:

* An identifier that matches no account is tracked exactly like a real one, so
  the lock (and the timing and message that come with it) cannot be used to find
  out which accounts exist.
* Keys are HMACs, so the table never holds an email address or username.
* A lock is capped at 15 minutes by default. That bounds how long someone who
  merely knows a victim's email can keep them out; the victim can still reset
  their password, which clears the lock.
* Failures older than LOGIN_FAILURE_WINDOW_SECONDS stop counting.
"""
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import LoginAttempt


def attempt_key(user, identifier):
    """A stable key for the account behind a login attempt.

    Username and email of the same account share one key, so alternating between
    them does not double the number of guesses.
    """
    subject = f"user:{user.pk}" if user is not None else f"identifier:{identifier.strip().lower()}"
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), subject.encode("utf-8"), hashlib.sha256,
    ).hexdigest()


def seconds_locked(key, now=None):
    """Whole seconds until the lock ends, or 0 when the account is not locked."""
    now = now or timezone.now()
    until = LoginAttempt.objects.filter(key_hash=key).values_list("locked_until", flat=True).first()
    if until is None or until <= now:
        return 0
    return max(1, int((until - now).total_seconds() + 0.999))


def lock_duration(failures):
    over = failures - settings.LOGIN_LOCK_THRESHOLD
    if over < 0:
        return 0
    return min(settings.LOGIN_LOCK_BASE_SECONDS * (2 ** min(over, 30)), settings.LOGIN_LOCK_MAX_SECONDS)


def record_failure(key, now=None):
    """Count a failed password; returns the new lock length in seconds (0 if none)."""
    now = now or timezone.now()
    for _ in range(2):
        try:
            with transaction.atomic():
                attempt, _created = LoginAttempt.objects.select_for_update().get_or_create(key_hash=key)
                window = timedelta(seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS)
                if attempt.last_failure_at and now - attempt.last_failure_at > window:
                    attempt.failures = 0
                attempt.failures += 1
                attempt.last_failure_at = now
                delay = lock_duration(attempt.failures)
                attempt.locked_until = now + timedelta(seconds=delay) if delay else None
                attempt.save()
                return delay
        except IntegrityError:
            # Two first failures raced to create the row; the retry finds it.
            continue
    return 0


def clear(key):
    LoginAttempt.objects.filter(key_hash=key).delete()


def clear_for_user(user):
    """Forget failures after a successful login, password reset or password change."""
    clear(attempt_key(user, ""))


def purge_stale_attempts(now=None):
    """Delete counters idle for a day (and not currently locking anyone)."""
    now = now or timezone.now()
    stale = LoginAttempt.objects.filter(last_failure_at__lt=now - timedelta(days=1)).exclude(
        locked_until__gt=now,
    )
    deleted, _ = stale.delete()
    return {"deleted": deleted}

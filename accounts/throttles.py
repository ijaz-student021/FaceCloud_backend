"""Request limits for sign-in and token refresh that do not lock a whole network out.

A classroom, a campus or a defence-day room shares **one public address**. A limit counted per address alone, and counted on every request, is used
up by a handful of people (the default "anonymous" limit is 100 requests an hour for the whole address), and then everyone is refused. These limits
count what actually signals abuse, and count it per thing being attacked:

* **Sign-in** counts *failed* attempts only (a correct password never uses any up), per address **and** account name together (``login``), plus a
  much higher ceiling of failures per address (``login_ip``) against one address trying many accounts. Guessing against one account from many addresses
  is limited separately and more strictly by the per-account backoff in ``accounts/lockout.py``.
* **Token refresh** counts per address **and** refresh token (``token_refresh``): thirty people refreshing every fifteen minutes each have their own
  allowance. A ceiling per address (``token_refresh_ip``) bounds abuse.

Every rate can be changed from the environment (``THROTTLE_RATE_<SCOPE>``); the defaults and the values recommended for a demo day and for production are in
``docs/deployment/rate-limits.md``.
"""
import hashlib

from rest_framework.throttling import SimpleRateThrottle


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _text(request, *names):
    """The first non-empty string among ``names`` in a JSON or form body (anything else is ignored), stripped and lower-cased."""
    data = getattr(request, "data", None)
    if hasattr(data, "get"):
        for name in names:
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ""


class _CountedThrottle(SimpleRateThrottle):
    """A rate limit whose count is only increased by ``record(request)``; ``allow_request`` just checks it.

    Subclasses say what to count under (``part``). An empty part means "nothing to key on", and then the request is not limited by this class.
    """

    def part(self, request):
        raise NotImplementedError

    def get_cache_key(self, request, view):
        part = self.part(request)
        return None if part is None else f"throttle_{self.scope}_{self.get_ident(request)}_{part}"

    def allow_request(self, request, view):
        self.key = self.get_cache_key(request, view)
        if self.rate is None or self.key is None:
            return True
        self.history = self.cache.get(self.key, [])
        self.now = self.timer()
        while self.history and self.history[-1] <= self.now - self.duration:
            self.history.pop()
        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return True

    def record(self, request):
        key = self.get_cache_key(request, None)
        if self.rate is None or key is None:
            return
        history = self.cache.get(key, [])
        now = self.timer()
        history = [moment for moment in history if moment > now - self.duration]
        history.insert(0, now)
        self.cache.set(key, history, self.duration)


class FailedSignInPerAccountThrottle(_CountedThrottle):
    """Failed sign-ins per (address, account name): ``login``."""

    scope = "login"

    def part(self, request):
        name = _text(request, "identifier", "username", "email")
        return _digest(name) if name else "none"


class FailedSignInPerAddressThrottle(_CountedThrottle):
    """Failed sign-ins per address, across every account name: ``login_ip``."""

    scope = "login_ip"

    def part(self, request):
        return "all"


class TokenRefreshPerTokenThrottle(SimpleRateThrottle):
    """Refresh requests per (address, refresh token): ``token_refresh``. Counts every request, because each one is a refresh."""

    scope = "token_refresh"

    def get_cache_key(self, request, view):
        token = _text(request, "refresh")
        return f"throttle_{self.scope}_{self.get_ident(request)}_{_digest(token) if token else 'none'}"


class TokenRefreshPerAddressThrottle(SimpleRateThrottle):
    """Refresh requests per address, a high ceiling: ``token_refresh_ip``."""

    scope = "token_refresh_ip"

    def get_cache_key(self, request, view):
        return f"throttle_{self.scope}_{self.get_ident(request)}"


def record_failed_sign_in(request, throttles):
    """Count one failed sign-in against every counted throttle of the view."""
    for throttle in throttles:
        if isinstance(throttle, _CountedThrottle):
            throttle.record(request)

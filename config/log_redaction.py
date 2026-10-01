"""Keep credential-bearing URL paths out of application logs.

Some links carry their own credential right in the path -- a share link
(`/api/shares/public/<token>/`) or an email-change verification link
(`/api/auth/verify-email/change/<uidb64>/<token>/`, self-executing on GET).
Django logs the request path for every 4xx/5xx (and the development server
logs every request), so without this, anyone who can read the logs can use
the link themselves.

Redaction is done in the log-record factory, so it applies to every record
whichever logger, handler or formatter ends up emitting it, and cannot be
bypassed by a logging configuration that forgets to attach a filter.

The pure `redact` function is also used by `gunicorn.conf.py` for the access log.

Add a new credential-bearing route here, not as a one-off special case --
this list is the single place every such route must be registered.
"""
import logging
import re

REDACTED = "[redacted]"

# Each pattern's own group 1 is "everything up to and including the route",
# so redact() only ever replaces the credential segment(s) that follow it.
# The plain marker (no regex) is used first as a cheap substring pre-check.
_ROUTES = (
    # The path segment after /shares/public/, up to the next slash, quote,
    # whitespace, query string or fragment.
    ("shares/public/", re.compile(r"(/shares/public/)[^/\s\"'?#]+")),
    # Both path segments after /verify-email/change/ (uidb64 and token),
    # since either alone still identifies/authorizes the account.
    ("verify-email/change/", re.compile(r"(/verify-email/change/)[^\s\"'?#]+")),
)

_installed = False


def redact(text):
    for _, pattern in _ROUTES:
        text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
    return text


def _mentions_marker(value):
    if isinstance(value, str):
        return any(marker in value for marker, _ in _ROUTES)
    if isinstance(value, bytes):
        return any(marker.encode() in value for marker, _ in _ROUTES)
    # Request objects passed as log arguments expose the path.
    path = getattr(value, "path", None)
    return isinstance(path, str) and any(marker in path for marker, _ in _ROUTES)


def _needs_redaction(record):
    if isinstance(record.msg, str) and any(marker in record.msg for marker, _ in _ROUTES):
        return True
    args = record.args
    if isinstance(args, dict):
        args = tuple(args.values())
    if isinstance(args, tuple):
        return any(_mentions_marker(arg) for arg in args)
    return False


def install():
    """Wrap the log-record factory once per process."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            if _needs_redaction(record):
                record.msg = redact(record.getMessage())
                record.args = None
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return record

    logging.setLogRecordFactory(factory)
    _installed = True

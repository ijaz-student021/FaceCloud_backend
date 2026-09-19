"""Keep share-link tokens out of application logs.

A share link is `/api/shares/public/<token>/`: the token *is* the credential, and
Django logs the request path for every 4xx/5xx (and the development server logs
every request). Without this, anyone who can read the logs can open the links.

Redaction is done in the log-record factory, so it applies to every record
whichever logger, handler or formatter ends up emitting it, and cannot be
bypassed by a logging configuration that forgets to attach a filter.

The pure `redact` function is also used by `gunicorn.conf.py` for the access log.
"""
import logging
import re

# The path segment that follows /shares/public/ up to the next slash, quote,
# whitespace, query string or fragment.
SHARE_TOKEN = re.compile(r"(/shares/public/)[^/\s\"'?#]+")
MARKER = "shares/public/"
REDACTED = "[redacted]"

_installed = False


def redact(text):
    return SHARE_TOKEN.sub(lambda match: match.group(1) + REDACTED, text)


def _mentions_marker(value):
    if isinstance(value, str):
        return MARKER in value
    if isinstance(value, bytes):
        return MARKER.encode() in value
    # Request objects passed as log arguments expose the path.
    path = getattr(value, "path", None)
    return isinstance(path, str) and MARKER in path


def _needs_redaction(record):
    if isinstance(record.msg, str) and MARKER in record.msg:
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

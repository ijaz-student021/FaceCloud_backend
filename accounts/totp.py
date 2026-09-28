"""RFC 6238 TOTP: the six-digit codes an authenticator app (Google Authenticator,
Authy, 1Password, ...) generates every 30 seconds.

Implemented directly with the standard library's ``hmac``/``hashlib`` rather than
adding a new dependency for a short, precisely specified algorithm (RFC 4226's HOTP,
time-stepped per RFC 6238). No network calls, no state; every function here is pure.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD_SECONDS = 30
SECRET_BYTES = 20  # 160 bits: RFC 4226's recommended HOTP key length


def generate_secret():
    """A fresh random base32 secret, the form every authenticator app expects."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")


def _hotp(secret, counter):
    """RFC 4226 HOTP: the code for one specific counter value."""
    padded = secret.upper() + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** DIGITS)).zfill(DIGITS)


def current_counter(when=None):
    return int((when if when is not None else time.time()) // PERIOD_SECONDS)


def generate_code(secret, when=None):
    """The code valid right now (or at ``when``, a unix timestamp) -- for tests only."""
    return _hotp(secret, current_counter(when))


def verify_code(secret, code, *, last_used_counter=None, window=1, when=None):
    """``(is_valid, counter_used)``.

    ``window`` tolerates this many 30-second steps of clock drift either side of
    now. A ``last_used_counter`` at or after a candidate counter rejects it --
    the same code (or an older one) cannot be replayed once it has been accepted.
    """
    code = (code or "").strip()
    if not code.isdigit() or len(code) != DIGITS:
        return False, None
    center = current_counter(when)
    for offset in range(-window, window + 1):
        counter = center + offset
        if last_used_counter is not None and counter <= last_used_counter:
            continue
        if hmac.compare_digest(_hotp(secret, counter), code):
            return True, counter
    return False, None


def provisioning_uri(secret, account_name, issuer):
    """The ``otpauth://`` URI an authenticator app scans as a QR code."""
    label = quote(f"{issuer}:{account_name}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_SECONDS}"
    )

"""Encrypts a user's TOTP secret at rest.

A separate module from ``config/image_crypto.py`` and ``identities/storage.py``:
those are chunked/streaming and named for their own domains (image files, face
embeddings). A TOTP secret is one short string, so it uses the ``cryptography``
package's high-level Fernet recipe directly (already a project dependency), keyed
by its own dedicated ``TWO_FACTOR_SECRET_KEY`` -- never ``DJANGO_SECRET_KEY`` or
the embedding/image keys, so rotating one can never lock or expose another
(the same rule ``config/production_guard.py`` enforces for those).
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class TwoFactorSecretError(Exception):
    """The stored secret could not be decrypted (wrong/rotated key, or tampering)."""


def _fernet_key(secret):
    # Fernet needs a 32-byte url-safe-base64 key; this derives one from whatever
    # string TWO_FACTOR_SECRET_KEY holds, so any sufficiently long, random value
    # works there, matching how the other *_KEY settings in this project are
    # plain configured strings rather than pre-formatted key material.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(secret):
    """Encrypt a base32 TOTP secret for storage in ``TwoFactorCredential.encrypted_secret``."""
    return Fernet(_fernet_key(settings.TWO_FACTOR_SECRET_KEY)).encrypt(secret.encode("ascii"))


def decrypt_secret(payload):
    try:
        return Fernet(_fernet_key(settings.TWO_FACTOR_SECRET_KEY)).decrypt(bytes(payload)).decode("ascii")
    except InvalidToken as error:
        raise TwoFactorSecretError("The stored two-factor secret could not be decrypted safely.") from error

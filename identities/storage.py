"""Protection of biometric templates (embeddings, group centroids) at rest.

Current format (``FCID2``): AES-256-GCM.

    b"FCID2" | key id (1 byte) | nonce (12 bytes) | ciphertext | GCM tag (16)

The header (format marker and key id) is authenticated as associated data, so
it cannot be swapped or edited. The AES key is derived from the configured
secret with HKDF-SHA256, bound to the key id. Key ids make rotation possible:
new records are written under ``IDENTITY_EMBEDDING_KEY_ID`` while records
written under keys listed in ``IDENTITY_EMBEDDING_PREVIOUS_KEYS`` stay
readable until ``manage.py reencrypt_identity_embeddings`` upgrades them.

Legacy format (``FCID1``): a hand-built keyed-BLAKE2s stream with an
HMAC-SHA256 tag and no key id. It is read-only here; nothing writes it any
more. Its tag identifies which configured key protected it.
"""

import functools
import hashlib
import hmac
import os
import struct

import numpy as np
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import ValidationError

MAGIC_V1 = b"FCID1"
MAGIC_V2 = b"FCID2"
_MAGIC_LENGTH = 5
_V2_HEADER_LENGTH = _MAGIC_LENGTH + 1
_NONCE_LENGTH = 12
_TAG_LENGTH = 16
_V1_MINIMUM_LENGTH = _MAGIC_LENGTH + 16 + 32


def _invalid():
    return ValidationError("A protected identity embedding is invalid.")


def current_key_id():
    key_id = int(settings.IDENTITY_EMBEDDING_KEY_ID)
    if not 1 <= key_id <= 255:
        raise ImproperlyConfigured("IDENTITY_EMBEDDING_KEY_ID must be between 1 and 255.")
    return key_id


def _configured_secrets():
    """``[(key_id, secret)]`` with the current key first."""
    current = current_key_id()
    secrets = [(current, settings.IDENTITY_EMBEDDING_KEY)]
    secrets.extend(
        (key_id, secret)
        for key_id, secret in settings.IDENTITY_EMBEDDING_PREVIOUS_KEYS.items()
        if key_id != current
    )
    return secrets


@functools.lru_cache(maxsize=32)
def _aead(secret, key_id):
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"facecloud/identity-embedding/v2/key-" + str(key_id).encode("ascii"),
    ).derive(secret.encode("utf-8"))
    return AESGCM(key)


def protect_embedding(vector):
    plain = np.asarray(vector, dtype="<f4").reshape(-1).tobytes()
    key_id = current_key_id()
    header = MAGIC_V2 + bytes([key_id])
    nonce = os.urandom(_NONCE_LENGTH)
    sealed = _aead(settings.IDENTITY_EMBEDDING_KEY, key_id).encrypt(nonce, plain, header)
    return header + nonce + sealed


def _decrypt_v2(payload):
    if len(payload) < _V2_HEADER_LENGTH + _NONCE_LENGTH + _TAG_LENGTH:
        raise _invalid()
    header = payload[:_V2_HEADER_LENGTH]
    nonce = payload[_V2_HEADER_LENGTH:_V2_HEADER_LENGTH + _NONCE_LENGTH]
    sealed = payload[_V2_HEADER_LENGTH + _NONCE_LENGTH:]
    key_id = payload[_MAGIC_LENGTH]
    secret = dict(_configured_secrets()).get(key_id)
    if secret is None:
        raise _invalid()
    try:
        return _aead(secret, key_id).decrypt(nonce, sealed, header)
    except InvalidTag:
        raise _invalid() from None


def _v1_key(secret):
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _v1_stream(key, nonce, length):
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hashlib.blake2s(nonce + struct.pack(">I", counter), key=key).digest())
        counter += 1
    return bytes(output[:length])


def _decrypt_v1(payload):
    if len(payload) < _V1_MINIMUM_LENGTH:
        raise _invalid()
    nonce, tag, ciphertext = payload[5:21], payload[21:53], payload[53:]
    for _key_id, secret in _configured_secrets():
        key = _v1_key(secret)
        expected = hmac.new(key, MAGIC_V1 + nonce + ciphertext, hashlib.sha256).digest()
        if hmac.compare_digest(tag, expected):
            return bytes(a ^ b for a, b in zip(ciphertext, _v1_stream(key, nonce, len(ciphertext))))
    raise _invalid()


def reveal_embedding(payload, expected_dimension):
    payload = bytes(payload)
    marker = payload[:_MAGIC_LENGTH]
    if marker == MAGIC_V2:
        plain = _decrypt_v2(payload)
    elif marker == MAGIC_V1:
        plain = _decrypt_v1(payload)
    else:
        raise _invalid()
    vector = np.frombuffer(plain, dtype="<f4").copy()
    if vector.size != expected_dimension or not np.isfinite(vector).all():
        raise _invalid()
    return vector


def payload_format(payload):
    """``("v2", key_id)``, ``("v1", None)`` or ``("unknown", None)``; never decrypts."""
    payload = bytes(payload)
    marker = payload[:_MAGIC_LENGTH]
    if marker == MAGIC_V2 and len(payload) > _MAGIC_LENGTH:
        return "v2", payload[_MAGIC_LENGTH]
    if marker == MAGIC_V1:
        return "v1", None
    return "unknown", None


def is_current_payload(payload):
    return payload_format(payload) == ("v2", current_key_id())

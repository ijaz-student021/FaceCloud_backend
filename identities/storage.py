import hashlib
import hmac
import os
import struct

import numpy as np
from django.conf import settings
from rest_framework.exceptions import ValidationError


def _key():
    return hashlib.sha256(settings.IDENTITY_EMBEDDING_KEY.encode("utf-8")).digest()


def _stream(key, nonce, length):
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hashlib.blake2s(nonce + struct.pack(">I", counter), key=key).digest())
        counter += 1
    return bytes(output[:length])


def protect_embedding(vector):
    payload = np.asarray(vector, dtype="<f4").reshape(-1).tobytes()
    nonce = os.urandom(16)
    key = _key()
    ciphertext = bytes(a ^ b for a, b in zip(payload, _stream(key, nonce, len(payload))))
    tag = hmac.new(key, b"FCID1" + nonce + ciphertext, hashlib.sha256).digest()
    return b"FCID1" + nonce + tag + ciphertext


def reveal_embedding(payload, expected_dimension):
    payload = bytes(payload)
    if len(payload) < 53 or payload[:5] != b"FCID1":
        raise ValidationError("A protected identity embedding is invalid.")
    nonce, tag, ciphertext = payload[5:21], payload[21:53], payload[53:]
    key = _key()
    expected = hmac.new(key, b"FCID1" + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValidationError("A protected identity embedding is invalid.")
    plain = bytes(a ^ b for a, b in zip(ciphertext, _stream(key, nonce, len(ciphertext))))
    vector = np.frombuffer(plain, dtype="<f4").copy()
    if vector.size != expected_dimension or not np.isfinite(vector).all():
        raise ValidationError("A protected identity embedding is invalid.")
    return vector

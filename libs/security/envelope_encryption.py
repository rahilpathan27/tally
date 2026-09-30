"""Local envelope encryption for vault payloads; production must use a managed KMS."""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_NONCE_SIZE = 12
_DATA_KEY_SIZE = 32
_TAG_SIZE = 16
_HEADER_SIZE = 1 + _NONCE_SIZE + _DATA_KEY_SIZE + _TAG_SIZE + _NONCE_SIZE


class EnvelopeCipher:
    """Encrypt each payload under a fresh data key, then wrap that key under a local KEK."""

    def __init__(self, key_encryption_key: bytes) -> None:
        if len(key_encryption_key) != 32:
            raise ValueError("local key-encryption key must be exactly 32 bytes")
        self._key_encryption_key = AESGCM(key_encryption_key)

    def encrypt(self, context: str, plaintext: bytes) -> bytes:
        if not context or not plaintext:
            raise ValueError("context and plaintext are required")
        associated_data = context.encode("utf-8")
        data_key = AESGCM.generate_key(bit_length=256)
        wrap_nonce = os.urandom(_NONCE_SIZE)
        wrapped_key = self._key_encryption_key.encrypt(wrap_nonce, data_key, associated_data)
        data_nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(data_key).encrypt(data_nonce, plaintext, associated_data)
        return _VERSION + wrap_nonce + wrapped_key + data_nonce + ciphertext

    def decrypt(self, context: str, envelope: bytes) -> bytes:
        if not context or len(envelope) < _HEADER_SIZE + _TAG_SIZE:
            raise ValueError("invalid encrypted vault payload")
        if envelope[:1] != _VERSION:
            raise ValueError("unsupported encrypted vault payload version")
        wrap_nonce_start = 1
        wrapped_key_start = wrap_nonce_start + _NONCE_SIZE
        wrapped_key_end = wrapped_key_start + _DATA_KEY_SIZE + _TAG_SIZE
        data_nonce_start = wrapped_key_end
        ciphertext_start = data_nonce_start + _NONCE_SIZE
        associated_data = context.encode("utf-8")
        data_key = self._key_encryption_key.decrypt(
            envelope[wrap_nonce_start:wrapped_key_start],
            envelope[wrapped_key_start:wrapped_key_end],
            associated_data,
        )
        return AESGCM(data_key).decrypt(
            envelope[data_nonce_start:ciphertext_start],
            envelope[ciphertext_start:],
            associated_data,
        )

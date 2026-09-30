"""Local AES-GCM wrapper for merchant HMAC secrets; cloud deployments need KMS."""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_NONCE_SIZE = 12
_TAG_SIZE = 16


class ApiKeyCipher:
    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("API key encryption key must be exactly 32 bytes")
        self._cipher = AESGCM(master_key)

    def encrypt(self, key_id: str, plaintext: bytes) -> bytes:
        if not key_id or not plaintext:
            raise ValueError("key ID and secret are required")
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = self._cipher.encrypt(nonce, plaintext, key_id.encode("utf-8"))
        return _VERSION + nonce + ciphertext

    def decrypt(self, key_id: str, stored: bytes) -> bytes:
        if not key_id or len(stored) < 1 + _NONCE_SIZE + _TAG_SIZE + 1:
            raise ValueError("invalid encrypted API key")
        if stored[:1] != _VERSION:
            raise ValueError("unsupported encrypted API key version")
        nonce = stored[1 : 1 + _NONCE_SIZE]
        return self._cipher.decrypt(nonce, stored[1 + _NONCE_SIZE :], key_id.encode("utf-8"))

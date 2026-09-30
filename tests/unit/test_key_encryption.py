from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from libs.security.key_encryption import ApiKeyCipher

MASTER_KEY = b"x" * 32


def test_api_key_secret_round_trip_and_randomized_ciphertext() -> None:
    cipher = ApiKeyCipher(MASTER_KEY)
    secret = b"merchant-hmac-secret-material"
    first = cipher.encrypt("key-a", secret)
    second = cipher.encrypt("key-a", secret)
    assert first != second
    assert cipher.decrypt("key-a", first) == secret
    assert cipher.decrypt("key-a", second) == secret


def test_api_key_ciphertext_is_bound_to_key_id_and_authentic() -> None:
    cipher = ApiKeyCipher(MASTER_KEY)
    encrypted = cipher.encrypt("key-a", b"secret")
    with pytest.raises(InvalidTag):
        cipher.decrypt("key-b", encrypted)
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(InvalidTag):
        cipher.decrypt("key-a", tampered)


def test_invalid_key_material_and_ciphertext_fail_closed() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        ApiKeyCipher(b"short")
    cipher = ApiKeyCipher(MASTER_KEY)
    with pytest.raises(ValueError, match="invalid encrypted"):
        cipher.decrypt("key-a", b"\x01short")

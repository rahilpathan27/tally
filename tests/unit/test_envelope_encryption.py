from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from libs.security.envelope_encryption import EnvelopeCipher


def test_envelope_encrypts_with_a_fresh_data_key_and_round_trips() -> None:
    cipher = EnvelopeCipher(b"k" * 32)
    plaintext = b"4242424242424242"
    first = cipher.encrypt("token-1", plaintext)
    second = cipher.encrypt("token-1", plaintext)
    assert first != second
    assert plaintext not in first
    assert cipher.decrypt("token-1", first) == plaintext
    assert cipher.decrypt("token-1", second) == plaintext


def test_envelope_binds_ciphertext_to_token_and_detects_tampering() -> None:
    cipher = EnvelopeCipher(b"k" * 32)
    envelope = cipher.encrypt("token-1", b"synthetic-card-data")
    with pytest.raises(InvalidTag):
        cipher.decrypt("token-2", envelope)
    tampered = envelope[:-1] + bytes([envelope[-1] ^ 1])
    with pytest.raises(InvalidTag):
        cipher.decrypt("token-1", tampered)


def test_envelope_requires_valid_kek_and_payload() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        EnvelopeCipher(b"short")
    cipher = EnvelopeCipher(b"k" * 32)
    with pytest.raises(ValueError, match="invalid encrypted"):
        cipher.decrypt("token-1", b"short")

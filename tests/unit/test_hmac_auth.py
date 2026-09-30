from __future__ import annotations

import pytest
from libs.security.hmac_auth import (
    AuthenticationError,
    SignedRequest,
    sign_request,
    verify_request,
)

SECRET = b"local-test-secret-with-at-least-32-bytes"


def signed(**overrides: object) -> SignedRequest:
    method = str(overrides.get("method", "POST"))
    path = str(overrides.get("path", "/v1/payments?mode=test"))
    body = overrides.get("body", b'{"amount_minor":125}')
    assert isinstance(body, bytes)
    raw_timestamp = overrides.get("timestamp", 1_700_000_000)
    assert isinstance(raw_timestamp, int)
    timestamp = raw_timestamp
    nonce = str(overrides.get("nonce", "request-unique-001"))
    return SignedRequest(
        key_id=str(overrides.get("key_id", "key_test_1")),
        timestamp=timestamp,
        nonce=nonce,
        signature=str(
            overrides.get("signature", sign_request(SECRET, method, path, body, timestamp, nonce))
        ),
    )


def test_valid_signature_passes() -> None:
    verify_request(
        signed(),
        method="POST",
        path="/v1/payments?mode=test",
        body=b'{"amount_minor":125}',
        secret_lookup=lambda key_id: SECRET if key_id == "key_test_1" else None,
        now=1_700_000_000,
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/payments?mode=test", b'{"amount_minor":125}'),
        ("POST", "/v1/payments", b'{"amount_minor":125}'),
        ("POST", "/v1/payments?mode=test", b'{"amount_minor":126}'),
    ],
)
def test_tampered_request_fails(method: str, path: str, body: bytes) -> None:
    with pytest.raises(AuthenticationError):
        verify_request(
            signed(),
            method=method,
            path=path,
            body=body,
            secret_lookup=lambda _: SECRET,
            now=1_700_000_000,
        )


def test_unknown_key_and_stale_timestamp_fail() -> None:
    with pytest.raises(AuthenticationError):
        verify_request(
            signed(),
            method="POST",
            path="/v1/payments?mode=test",
            body=b'{"amount_minor":125}',
            secret_lookup=lambda _: None,
            now=1_700_000_000,
        )
    with pytest.raises(AuthenticationError):
        verify_request(
            signed(),
            method="POST",
            path="/v1/payments?mode=test",
            body=b'{"amount_minor":125}',
            secret_lookup=lambda _: SECRET,
            now=1_700_000_400,
        )


def test_short_hmac_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        sign_request(b"too-short", "POST", "/v1/payments", b"{}", 1, "nonce-1")

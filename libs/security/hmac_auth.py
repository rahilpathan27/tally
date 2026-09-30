"""HMAC request authentication primitives for the merchant API boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuthenticationError(ValueError):
    """A request failed authentication or replay-window validation."""


@dataclass(frozen=True, slots=True)
class SignedRequest:
    key_id: str
    timestamp: int
    nonce: str
    signature: str


def canonical_request(method: str, path: str, body: bytes, timestamp: int, nonce: str) -> bytes:
    """Build the v1 signing payload; query string is part of path and must be preserved."""
    if (
        not method
        or not path.startswith("/")
        or "\n" in method
        or "\n" in path
        or not nonce
        or "\n" in nonce
    ):
        raise ValueError("method and absolute request path are required")
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{body_hash}\n{timestamp}\n{nonce}".encode()


def sign_request(
    secret: bytes, method: str, path: str, body: bytes, timestamp: int, nonce: str
) -> str:
    if len(secret) < 32:
        raise ValueError("HMAC secrets must contain at least 32 bytes")
    return hmac.new(
        secret, canonical_request(method, path, body, timestamp, nonce), hashlib.sha256
    ).hexdigest()


def verify_request(
    request: SignedRequest,
    *,
    method: str,
    path: str,
    body: bytes,
    secret_lookup: Callable[[str], bytes | None],
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> None:
    """Validate key, timestamp, and MAC. Caller must atomically consume (key ID, nonce)."""
    current_time = int(time.time()) if now is None else now
    if tolerance_seconds < 0 or abs(current_time - request.timestamp) > tolerance_seconds:
        raise AuthenticationError("request timestamp is outside the allowed window")
    secret = secret_lookup(request.key_id)
    if secret is None or len(secret) < 32:
        raise AuthenticationError("invalid API key")
    if not _HEX_SHA256.fullmatch(request.signature):
        raise AuthenticationError("invalid request signature")
    expected = sign_request(secret, method, path, body, request.timestamp, request.nonce)
    if not hmac.compare_digest(expected, request.signature):
        raise AuthenticationError("invalid request signature")

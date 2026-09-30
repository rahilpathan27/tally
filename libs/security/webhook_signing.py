"""Webhook signatures: ``Tally-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "t.body")>``."""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "Tally-Signature"


def sign_payload(secret: bytes, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify_signature(
    secret: bytes,
    header: str,
    body: bytes,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    """Constant-time check of any ``v1`` signature, rejecting stale timestamps (replays)."""
    try:
        fields = [part.split("=", 1) for part in header.split(",")]
        timestamp = int(next(value for key, value in fields if key == "t"))
        candidates = [value for key, value in fields if key == "v1"]
    except (StopIteration, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds or not candidates:
        return False
    expected = sign_payload(secret, body, timestamp).split("v1=", 1)[1]
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)

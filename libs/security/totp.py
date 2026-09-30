"""RFC 6238 time-based one-time passwords (HMAC-SHA1, 30-second steps, 6 digits)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def hotp(key: bytes, counter: int, digits: int = 6) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    moment = time.time() if at is None else at
    return hotp(_key(secret), int(moment // step), digits)


def verify(
    secret: str, code: str, at: float | None = None, window: int = 1, step: int = 30
) -> int | None:
    """Return the matched time step (for replay tracking) or None. Constant-time comparison."""
    if not code.isdigit() or len(code) != 6:
        return None
    moment = time.time() if at is None else at
    current = int(moment // step)
    matched = None
    for counter in range(current - window, current + window + 1):
        if hmac.compare_digest(hotp(_key(secret), counter), code):
            matched = counter
    return matched


def provisioning_uri(secret: str, account: str, issuer: str = "Tally (simulation)") -> str:
    from urllib.parse import quote

    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"

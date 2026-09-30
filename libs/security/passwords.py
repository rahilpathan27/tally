"""Password hashing with scrypt (memory-hard), per-password salt, constant-time verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P, DKLEN = 2**14, 8, 1, 32


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("passwords must be at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return (
        f"scrypt${N}${R}${P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A fixed hash to verify against when the user does not exist (keeps timing uniform).
DUMMY_HASH = hash_password("dummy-password-for-timing")

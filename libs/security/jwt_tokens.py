"""Short-lived access tokens: ES256 only, ``kid`` rotation, issuer/audience checks.

The verifier pins the algorithm list to ES256 and resolves the key strictly by ``kid`` from the
configured key ring, which defeats ``alg=none``, HS256-with-public-key confusion and unknown-key
attacks. Retired keys stay in the ring for verification until their tokens expire.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ALGORITHM = "ES256"
ISSUER = "tally-backoffice"
AUDIENCE = "tally-console"


class TokenError(Exception):
    pass


@dataclass(slots=True)
class KeyRing:
    """``active`` signs; every key in ``keys`` verifies (rotation overlap)."""

    keys: dict[str, ec.EllipticCurvePrivateKey] = field(default_factory=dict)
    active: str = ""

    @classmethod
    def generate(cls) -> KeyRing:
        ring = cls()
        ring.rotate()
        return ring

    def rotate(self) -> str:
        kid = uuid.uuid4().hex[:16]
        self.keys[kid] = ec.generate_private_key(ec.SECP256R1())
        self.active = kid
        return kid

    def retire(self, kid: str) -> None:
        if kid == self.active:
            raise ValueError("cannot retire the active key")
        self.keys.pop(kid, None)

    def public_pem(self, kid: str) -> bytes:
        return (
            self.keys[kid]
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )


def issue(ring: KeyRing, subject: str, claims: dict[str, Any], ttl_seconds: int = 600) -> str:
    now = int(time.time())
    payload = {
        **claims,
        "sub": subject,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(
        payload, ring.keys[ring.active], algorithm=ALGORITHM, headers={"kid": ring.active}
    )


def verify(ring: KeyRing, token: str, *, leeway: int = 5) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError("malformed token") from exc
    if header.get("alg") != ALGORITHM:
        raise TokenError("algorithm not allowed")
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in ring.keys:
        raise TokenError("unknown key")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            ring.keys[kid].public_key(),
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=leeway,
            options={"require": ["exp", "iat", "nbf", "sub", "iss", "aud", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    return claims

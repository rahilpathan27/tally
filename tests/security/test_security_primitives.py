import base64
import json
import logging
import time

import httpx
import jwt as pyjwt
import pytest
from fastapi import FastAPI
from libs.observability.logging import RedactingJsonFormatter, redact, redact_text
from libs.security import jwt_tokens, totp
from libs.security.http import SecurityMiddleware
from libs.security.passwords import hash_password, verify_password

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("at", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_totp_matches_rfc_6238_vectors(at: int, code: str) -> None:
    assert totp.totp(RFC_SECRET, at=at) == code
    assert totp.verify(RFC_SECRET, code, at=at) is not None
    assert totp.verify(RFC_SECRET, code, at=at + 120) is None
    assert totp.verify(RFC_SECRET, "12345", at=at) is None


def test_password_hashing() -> None:
    encoded = hash_password("correct horse battery")
    assert encoded.startswith("scrypt$") and "correct" not in encoded
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("correct horse batterY", encoded)
    assert not verify_password("x", "md5$abc")
    with pytest.raises(ValueError):
        hash_password("short")


def _hs256_confusion(claims: dict[str, object], public_pem: bytes, kid: str) -> str:
    """Hand-craft the classic key-confusion token (PyJWT refuses to create it)."""
    import hashlib
    import hmac

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
        + "."
        + b64(json.dumps(claims).encode())
    )
    mac = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64(mac)}"


def test_jwt_rejects_known_attacks_and_supports_rotation() -> None:
    ring = jwt_tokens.KeyRing.generate()
    token = jwt_tokens.issue(ring, "user-1", {"roles": ["ops_analyst"]})
    assert jwt_tokens.verify(ring, token)["roles"] == ["ops_analyst"]

    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["roles"] = ["approver"]
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    attacks = {
        "tampered payload": f"{header}.{forged}.{signature}",
        "alg none": pyjwt.encode(claims, "", algorithm="none", headers={"kid": ring.active}),
        "hs256 with public key": _hs256_confusion(
            claims, ring.public_pem(ring.active), ring.active
        ),
        "unknown kid": pyjwt.encode(
            claims, ring.keys[ring.active], algorithm="ES256", headers={"kid": "nope"}
        ),
        "expired": jwt_tokens.issue(ring, "user-1", {}, ttl_seconds=-60),
        "wrong audience": pyjwt.encode(
            {**claims, "aud": "someone-else"},
            ring.keys[ring.active],
            algorithm="ES256",
            headers={"kid": ring.active},
        ),
        "garbage": "not.a.token",
    }
    for name, attack in attacks.items():
        with pytest.raises(jwt_tokens.TokenError):
            jwt_tokens.verify(ring, attack)
            pytest.fail(name)

    old_kid = ring.active
    ring.rotate()
    assert jwt_tokens.verify(ring, token)["sub"] == "user-1"  # overlap window
    ring.retire(old_kid)
    with pytest.raises(jwt_tokens.TokenError):
        jwt_tokens.verify(ring, token)


def test_logs_never_contain_pan_cvv_or_secrets(caplog: pytest.LogCaptureFixture) -> None:
    assert redact_text("card 4242 4242 4242 4242 declined") == "card [PAN ****4242] declined"
    assert redact_text("order 1234567890123 ok") == "order 1234567890123 ok"  # not Luhn-valid
    assert "s3cret" not in redact_text("password=s3cret&x=1")
    nested = redact({"pan": "4111111111111111", "note": "cvv: 123", "merchant_id": "m1"})
    assert nested["pan"] == "[REDACTED]" and "123" not in nested["note"]
    assert nested["merchant_id"] == "m1"
    record = logging.LogRecord(
        "t", logging.INFO, "f", 1, "paid with %s", ("5555555555554444",), None
    )
    record.authorization = "Bearer abc"
    record.payment_id = "pay_1"
    line = RedactingJsonFormatter("test").format(record)
    assert "5555555555554444" not in line and "abc" not in line and "pay_1" in line


def test_security_middleware_headers_size_and_content_type() -> None:
    app = FastAPI()

    @app.post("/echo")
    async def echo(body: dict[str, int]) -> dict[str, int]:
        return body

    wrapped = SecurityMiddleware(app, max_body_bytes=64)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://t"
        ) as client:
            ok = await client.post("/echo", json={"a": 1})
            assert ok.status_code == 200
            assert ok.headers["x-content-type-options"] == "nosniff"
            assert "default-src 'none'" in ok.headers["content-security-policy"]
            big = await client.post("/echo", json={"a": 1, "pad": "x" * 200})
            assert big.status_code == 413
            form = await client.post(
                "/echo", content=b"a=1", headers={"content-type": "text/plain"}
            )
            assert form.status_code == 415

    import asyncio

    asyncio.run(run())
    assert time.time() > 0

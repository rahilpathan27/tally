"""Back-office authentication: password + TOTP MFA, short-lived JWT, rotating refresh tokens.

Session cookies are ``HttpOnly; Secure; SameSite=Strict``. Mutations also require the
``X-CSRF-Token`` header to equal the ``tally_csrf`` cookie (double submit), so a cross-site
form cannot act with the user's cookies. Access tokens live ten minutes; refresh tokens rotate
on each use and presenting a used one revokes the whole family (theft detection).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from libs.observability.metrics import AUTH_EVENTS
from libs.security import jwt_tokens, totp
from libs.security.passwords import DUMMY_HASH, verify_password
from pydantic import BaseModel, ConfigDict, Field

ACCESS_TTL = 600
REFRESH_TTL = timedelta(hours=12)
MFA_TTL = 300
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
ACCESS_COOKIE = "tally_access"
REFRESH_COOKIE = "tally_refresh"
CSRF_COOKIE = "tally_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

router = APIRouter()


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    email: str
    roles: frozenset[str]
    merchant_id: str | None


class LoginRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    email: Annotated[str, Field(min_length=3, max_length=200)]
    password: Annotated[str, Field(min_length=1, max_length=200)]


class MfaRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    mfa_token: Annotated[str, Field(min_length=10, max_length=2000)]
    code: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


def _unauthorized(message: str = "Authentication failed.") -> HTTPException:
    return HTTPException(401, detail={"code": "UNAUTHENTICATED", "message": message})


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _pool(request: Request) -> asyncpg.Pool:
    return cast(asyncpg.Pool, request.app.state.pool)


def _ring(request: Request) -> jwt_tokens.KeyRing:
    return cast(jwt_tokens.KeyRing, request.app.state.key_ring)


async def _audit(pool: asyncpg.Pool, actor: str, action: str, details: dict[str, Any]) -> None:
    AUTH_EVENTS.labels(action).inc()
    await pool.execute(
        "SELECT audit_append($1, $2, $3, NULL, $4::jsonb)",
        actor,
        action,
        "backoffice-session",
        json.dumps(details),
    )


def _set_session(
    request: Request, response: Response, user: asyncpg.Record, refresh: str
) -> dict[str, Any]:
    roles = list(user["roles"])
    access = jwt_tokens.issue(
        _ring(request),
        str(user["user_id"]),
        {"email": user["email"], "roles": roles, "merchant_id": user["merchant_id"]},
        ttl_seconds=ACCESS_TTL,
    )
    csrf = secrets.token_urlsafe(24)
    secure = bool(getattr(request.app.state, "secure_cookies", True))
    response.set_cookie(
        ACCESS_COOKIE, access, max_age=ACCESS_TTL, httponly=True, secure=secure, samesite="strict"
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=int(REFRESH_TTL.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/auth",
    )
    response.set_cookie(CSRF_COOKIE, csrf, max_age=ACCESS_TTL, secure=secure, samesite="strict")
    return {
        "user_id": str(user["user_id"]),
        "email": user["email"],
        "roles": roles,
        "merchant_id": user["merchant_id"],
        "csrf_token": csrf,
        "expires_in": ACCESS_TTL,
    }


async def _new_refresh(pool: asyncpg.Pool, user_id: uuid.UUID, family: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    await pool.execute(
        """INSERT INTO backoffice_refresh_tokens(token_hash, family_id, user_id, expires_at)
           VALUES ($1, $2, $3, $4)""",
        _hash(token),
        family,
        user_id,
        datetime.now(UTC) + REFRESH_TTL,
    )
    return token


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    pool = _pool(request)
    email = body.email.strip().lower()
    user = await pool.fetchrow("SELECT * FROM backoffice_users WHERE email = $1", email)
    # Verify against a dummy hash for unknown users so timing does not reveal accounts.
    password_ok = verify_password(body.password, user["password_hash"] if user else DUMMY_HASH)
    now = datetime.now(UTC)
    if user is None or user["status"] != "active":
        await _audit(pool, email, "login_failed", {"reason": "unknown_or_disabled"})
        raise _unauthorized()
    if user["locked_until"] is not None and user["locked_until"] > now:
        await _audit(pool, email, "login_blocked", {"reason": "locked"})
        raise _unauthorized("Account temporarily locked.")
    if not password_ok:
        failed = user["failed_logins"] + 1
        await pool.execute(
            """UPDATE backoffice_users SET failed_logins = $2::int,
                   locked_until = CASE WHEN $2::int >= $3::int THEN $4::timestamptz
                                       ELSE locked_until END
               WHERE user_id = $1""",
            user["user_id"],
            failed,
            MAX_FAILED_LOGINS,
            now + LOCKOUT,
        )
        await _audit(pool, email, "login_failed", {"reason": "password", "failed": failed})
        raise _unauthorized()
    await pool.execute(
        "UPDATE backoffice_users SET failed_logins = 0, locked_until = NULL WHERE user_id = $1",
        user["user_id"],
    )
    if user["mfa_enabled"]:
        token = jwt_tokens.issue(
            _ring(request), str(user["user_id"]), {"purpose": "mfa"}, ttl_seconds=MFA_TTL
        )
        return {"mfa_required": True, "mfa_token": token}
    await _audit(pool, email, "login", {"mfa": False})
    refresh = await _new_refresh(pool, user["user_id"], uuid.uuid4())
    return _set_session(request, response, user, refresh)


@router.post("/auth/mfa")
async def complete_mfa(body: MfaRequest, request: Request, response: Response) -> dict[str, Any]:
    pool = _pool(request)
    try:
        claims = jwt_tokens.verify(_ring(request), body.mfa_token)
    except jwt_tokens.TokenError as exc:
        raise _unauthorized() from exc
    if claims.get("purpose") != "mfa":
        raise _unauthorized()
    user = await pool.fetchrow(
        "SELECT * FROM backoffice_users WHERE user_id = $1 AND status = 'active'",
        uuid.UUID(claims["sub"]),
    )
    if user is None or user["totp_secret_ciphertext"] is None:
        raise _unauthorized()
    secret = request.app.state.secret_cipher.decrypt(
        f"totp:{user['user_id']}", bytes(user["totp_secret_ciphertext"])
    ).decode()
    step = totp.verify(secret, body.code)
    # A code is single-use: reject steps at or before the last accepted one (replay).
    if step is None or (user["last_totp_step"] is not None and step <= user["last_totp_step"]):
        await _audit(pool, user["email"], "mfa_failed", {})
        raise _unauthorized()
    await pool.execute(
        "UPDATE backoffice_users SET last_totp_step = $2, last_login_at = clock_timestamp() "
        "WHERE user_id = $1",
        user["user_id"],
        step,
    )
    await _audit(pool, user["email"], "login", {"mfa": True})
    refresh = await _new_refresh(pool, user["user_id"], uuid.uuid4())
    return _set_session(request, response, user, refresh)


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response) -> dict[str, Any]:
    pool = _pool(request)
    presented = request.cookies.get(REFRESH_COOKIE, "")
    if not presented:
        raise _unauthorized()
    failure: str | None = None
    user: asyncpg.Record | None = None
    new_refresh = ""
    # The row lock serialises concurrent refreshes of one token. The revocation must commit, so
    # failures are raised only after the transaction ends.
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            "SELECT * FROM backoffice_refresh_tokens WHERE token_hash = $1 FOR UPDATE",
            _hash(presented),
        )
        if row is None:
            failure = "Authentication failed."
        elif row["used_at"] is not None or row["revoked_at"] is not None:
            # Reuse of a rotated token means it was stolen: revoke every token in the family.
            await connection.execute(
                """UPDATE backoffice_refresh_tokens SET revoked_at = clock_timestamp()
                   WHERE family_id = $1 AND revoked_at IS NULL""",
                row["family_id"],
            )
            await connection.execute(
                "SELECT audit_append($1, 'refresh_token_reuse', 'backoffice-session', NULL, $2)",
                str(row["user_id"]),
                json.dumps({"family_id": str(row["family_id"])}),
            )
            failure = "Session revoked."
            AUTH_EVENTS.labels("refresh_token_reuse").inc()
        elif row["expires_at"] <= datetime.now(UTC):
            failure = "Session expired."
        else:
            await connection.execute(
                "UPDATE backoffice_refresh_tokens SET used_at = clock_timestamp() "
                "WHERE token_hash = $1",
                row["token_hash"],
            )
            user = await connection.fetchrow(
                "SELECT * FROM backoffice_users WHERE user_id = $1 AND status = 'active'",
                row["user_id"],
            )
            if user is not None:
                new_refresh = secrets.token_urlsafe(32)
                await connection.execute(
                    """INSERT INTO backoffice_refresh_tokens(
                           token_hash, family_id, user_id, expires_at
                       ) VALUES ($1, $2, $3, $4)""",
                    _hash(new_refresh),
                    row["family_id"],
                    user["user_id"],
                    datetime.now(UTC) + REFRESH_TTL,
                )
    if failure is not None or user is None:
        raise _unauthorized(failure or "Authentication failed.")
    return _set_session(request, response, user, new_refresh)


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    presented = request.cookies.get(REFRESH_COOKIE, "")
    if presented:
        await _pool(request).execute(
            """UPDATE backoffice_refresh_tokens SET revoked_at = clock_timestamp()
               WHERE family_id = (SELECT family_id FROM backoffice_refresh_tokens
                                  WHERE token_hash = $1)""",
            _hash(presented),
        )
    for cookie in (ACCESS_COOKIE, CSRF_COOKIE):
        response.delete_cookie(cookie)
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return {"status": "logged_out"}


async def current_principal(request: Request) -> Principal:
    token = request.cookies.get(ACCESS_COOKIE, "")
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:]
    if not token:
        raise _unauthorized()
    try:
        claims = jwt_tokens.verify(_ring(request), token)
    except jwt_tokens.TokenError as exc:
        raise _unauthorized() from exc
    if claims.get("purpose") == "mfa":
        raise _unauthorized()
    if request.method not in SAFE_METHODS and request.cookies.get(ACCESS_COOKIE):
        cookie = request.cookies.get(CSRF_COOKIE, "")
        header_token = request.headers.get("x-csrf-token", "")
        if not cookie or not hmac.compare_digest(cookie, header_token):
            raise HTTPException(403, detail={"code": "CSRF", "message": "CSRF check failed."})
    return Principal(
        user_id=str(claims["sub"]),
        email=str(claims.get("email", "")),
        roles=frozenset(claims.get("roles", [])),
        merchant_id=claims.get("merchant_id"),
    )


@router.get("/auth/session")
async def session(request: Request) -> dict[str, Any]:
    """Who is signed in (drives navigation; every route still enforces its own permission)."""
    principal = await current_principal(request)
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "roles": sorted(principal.roles),
        "merchant_id": principal.merchant_id,
    }

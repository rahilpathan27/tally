"""Create one-time merchant HMAC credentials and revoke active keys."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

import asyncpg
from libs.security.key_encryption import ApiKeyCipher


def _configuration() -> tuple[str, ApiKeyCipher]:
    database_url = os.environ.get("TALLY_DATABASE_URL")
    encoded_key = os.environ.get("TALLY_API_KEY_ENCRYPTION_KEY")
    if not database_url or not encoded_key:
        raise RuntimeError("TALLY_DATABASE_URL and TALLY_API_KEY_ENCRYPTION_KEY must be configured")
    try:
        master_key = base64.b64decode(encoded_key, altchars=b"-_", validate=True)
    except ValueError as exc:
        raise RuntimeError("TALLY_API_KEY_ENCRYPTION_KEY must be base64-encoded") from exc
    return database_url, ApiKeyCipher(master_key)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a merchant API key")
    create.add_argument("--merchant-id", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--scope", action="append", required=True)
    create.add_argument("--mode", choices=("test", "live"), default="test")
    create.add_argument("--expires-in-days", type=int, default=90)
    revoke = commands.add_parser("revoke", help="revoke a merchant API key")
    revoke.add_argument("--key-id", required=True)
    rotate = commands.add_parser("rotate", help="replace an active key atomically")
    rotate.add_argument("--key-id", required=True, help="active key to replace")
    rotate.add_argument("--expires-in-days", type=int, default=90)
    return parser


async def _create_key(args: argparse.Namespace, database_url: str, cipher: ApiKeyCipher) -> None:
    if args.expires_in_days <= 0 or args.expires_in_days > 3650:
        raise ValueError("expires-in-days must be between 1 and 3650")
    key_id = f"tly_{args.mode}_{secrets.token_urlsafe(12)}"
    secret = secrets.token_bytes(32)
    encoded_secret = base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
    expiry = datetime.now(UTC) + timedelta(days=args.expires_in_days)
    connection = await asyncpg.connect(database_url)
    try:
        async with connection.transaction():
            await connection.execute(
                """INSERT INTO merchants(merchant_id, display_name)
                   VALUES ($1, $2) ON CONFLICT (merchant_id) DO NOTHING""",
                args.merchant_id,
                args.display_name,
            )
            merchant = await connection.fetchrow(
                "SELECT status FROM merchants WHERE merchant_id = $1 FOR UPDATE",
                args.merchant_id,
            )
            if merchant is None or merchant["status"] != "active":
                raise ValueError("merchant does not exist or is not active")
            await connection.execute(
                """INSERT INTO merchant_api_keys(
                       key_id, merchant_id, secret_ciphertext, scopes, mode, expires_at
                   ) VALUES ($1, $2, $3, $4, $5, $6)""",
                key_id,
                args.merchant_id,
                cipher.encrypt(key_id, secret),
                args.scope,
                args.mode,
                expiry,
            )
            await connection.execute(
                "SELECT set_config('app.merchant_id', $1, true)", args.merchant_id
            )
            await connection.fetchval(
                """SELECT gateway_append_audit_event(
                       $1, NULL, 'api_key.create', $2, $3::jsonb
                   )""",
                args.merchant_id,
                key_id,
                '{"mode":"' + args.mode + '"}',
            )
    finally:
        await connection.close()
    print(f"key_id={key_id}")
    print(f"secret={encoded_secret}")
    print(f"expires_at={expiry.isoformat()}")


async def _revoke_key(key_id: str, database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        async with connection.transaction():
            row = await connection.fetchrow(
                """UPDATE merchant_api_keys SET revoked_at = clock_timestamp()
                    WHERE key_id = $1 AND revoked_at IS NULL
                    RETURNING merchant_id""",
                key_id,
            )
            if row is None:
                raise ValueError("active API key was not found")
            merchant_id = str(row["merchant_id"])
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            await connection.fetchval(
                """SELECT gateway_append_audit_event(
                       $1, NULL, 'api_key.revoke', $2, '{}'::jsonb
                   )""",
                merchant_id,
                key_id,
            )
    finally:
        await connection.close()
    print(f"revoked key_id={key_id}")


async def _rotate_key(args: argparse.Namespace, database_url: str, cipher: ApiKeyCipher) -> None:
    if args.expires_in_days <= 0 or args.expires_in_days > 3650:
        raise ValueError("expires-in-days must be between 1 and 3650")
    connection = await asyncpg.connect(database_url)
    key_id = ""
    expiry = datetime.now(UTC) + timedelta(days=args.expires_in_days)
    secret = secrets.token_bytes(32)
    encoded_secret = base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
    try:
        async with connection.transaction():
            prior = await connection.fetchrow(
                """SELECT merchant_id, scopes, mode FROM merchant_api_keys
                    WHERE key_id = $1 AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > clock_timestamp())
                    FOR UPDATE""",
                args.key_id,
            )
            if prior is None:
                raise ValueError("active API key was not found")
            mode = str(prior["mode"])
            key_id = f"tly_{mode}_{secrets.token_urlsafe(12)}"
            merchant_id = str(prior["merchant_id"])
            await connection.execute(
                """UPDATE merchant_api_keys SET revoked_at = clock_timestamp()
                    WHERE key_id = $1 AND revoked_at IS NULL""",
                args.key_id,
            )
            await connection.execute(
                """INSERT INTO merchant_api_keys(
                       key_id, merchant_id, secret_ciphertext, scopes, mode, expires_at
                   ) VALUES ($1, $2, $3, $4, $5, $6)""",
                key_id,
                merchant_id,
                cipher.encrypt(key_id, secret),
                prior["scopes"],
                mode,
                expiry,
            )
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            event_data = json.dumps(
                {"old_key_id": args.key_id, "new_key_id": key_id}, separators=(",", ":")
            )
            await connection.fetchval(
                """SELECT gateway_append_audit_event(
                       $1, NULL, 'api_key.rotate', $2, $3::jsonb
                   )""",
                merchant_id,
                key_id,
                event_data,
            )
    finally:
        await connection.close()
    print(f"key_id={key_id}")
    print(f"secret={encoded_secret}")
    print(f"expires_at={expiry.isoformat()}")


async def _run(args: argparse.Namespace) -> None:
    database_url, cipher = _configuration()
    if args.command == "create":
        await _create_key(args, database_url, cipher)
    elif args.command == "revoke":
        await _revoke_key(args.key_id, database_url)
    elif args.command == "rotate":
        await _rotate_key(args, database_url, cipher)
    else:
        raise RuntimeError("unsupported command")


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()

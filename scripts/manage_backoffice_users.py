"""Create back-office users and enrol TOTP MFA (local simulation).

The generated TOTP secret is printed once for enrolment in an authenticator app.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import uuid

import asyncpg
from libs.security import totp
from libs.security.key_encryption import ApiKeyCipher
from libs.security.passwords import hash_password


async def create_user(
    pool: asyncpg.Pool,
    cipher: ApiKeyCipher,
    email: str,
    password: str,
    roles: list[str],
    merchant_id: str | None = None,
    mfa: bool = True,
) -> tuple[uuid.UUID, str | None]:
    user_id = uuid.uuid4()
    secret = totp.new_secret() if mfa else None
    await pool.execute(
        """INSERT INTO backoffice_users(user_id, email, password_hash, roles, merchant_id,
                                        totp_secret_ciphertext, mfa_enabled)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        user_id,
        email.lower(),
        hash_password(password),
        roles,
        merchant_id,
        None if secret is None else cipher.encrypt(f"totp:{user_id}", secret.encode()),
        mfa,
    )
    await pool.execute(
        "SELECT audit_append('cli', 'user_created', $1, $2, $3::jsonb)",
        email.lower(),
        merchant_id,
        f'{{"roles": {roles!r}}}'.replace("'", '"'),
    )
    return user_id, secret


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", action="append", required=True)
    parser.add_argument("--merchant-id")
    parser.add_argument("--no-mfa", action="store_true")
    args = parser.parse_args()
    password = os.environ.get("TALLY_NEW_USER_PASSWORD")
    if not password:
        raise SystemExit("set TALLY_NEW_USER_PASSWORD (not passed on the command line)")
    key = base64.b64decode(os.environ["TALLY_BACKOFFICE_SECRET_KEY"], altchars=b"-_")

    async def run() -> None:
        pool = await asyncpg.create_pool(os.environ["TALLY_DATABASE_URL"], min_size=1, max_size=2)
        try:
            user_id, secret = await create_user(
                pool,
                ApiKeyCipher(key),
                args.email,
                password,
                args.role,
                args.merchant_id,
                not args.no_mfa,
            )
        finally:
            await pool.close()
        print(f"user_id={user_id}")
        if secret:
            print(f"totp_uri={totp.provisioning_uri(secret, args.email)}")

    asyncio.run(run())


if __name__ == "__main__":
    main()

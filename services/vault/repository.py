"""Persistence boundary for encrypted test-card tokens."""

from __future__ import annotations

import secrets

import asyncpg
from cryptography.exceptions import InvalidTag
from libs.security.envelope_encryption import EnvelopeCipher

from services.vault.model import TokenMetadata


class VaultRepository:
    def __init__(self, pool: asyncpg.Pool, cipher: EnvelopeCipher) -> None:
        self._pool = pool
        self._cipher = cipher

    async def tokenize(self, pan: str, expiry_month: int, expiry_year: int) -> TokenMetadata:
        token = f"vlt_{secrets.token_urlsafe(32)}"
        encrypted_pan = self._cipher.encrypt(token, pan.encode("ascii"))
        async with self._pool.acquire() as connection:
            await connection.execute(
                """INSERT INTO vault_cards(
                       token, encrypted_pan, bin_prefix, last4, expiry_month, expiry_year
                   ) VALUES ($1, $2, $3, $4, $5, $6)""",
                token,
                encrypted_pan,
                pan[:6],
                pan[-4:],
                expiry_month,
                expiry_year,
            )
        return TokenMetadata(token, pan[-4:], expiry_month, expiry_year)

    async def detokenize(self, token: str, caller_id: str, request_id: str) -> str:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT encrypted_pan FROM vault_cards WHERE token = $1 AND revoked_at IS NULL",
                token,
            )
            if row is None:
                raise KeyError("card token was not found")
            await connection.fetchval(
                "SELECT vault_append_access_event($1, $2, $3)", token, caller_id, request_id
            )
            try:
                pan = self._cipher.decrypt(token, bytes(row["encrypted_pan"]))
            except (InvalidTag, ValueError) as exc:
                raise RuntimeError("vault ciphertext could not be authenticated") from exc
        try:
            return pan.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("vault ciphertext is not an encoded card number") from exc

    async def health(self) -> None:
        async with self._pool.acquire() as connection:
            await connection.fetchval("SELECT 1")

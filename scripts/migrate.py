"""Apply versioned SQL migrations for one service database.

Files are named ``NNNN_description.sql`` and record their own version in the service's
``<service>_schema_migrations`` table inside the file's transaction. A version is applied
at most once; re-running the command is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import asyncpg

LOCAL = "postgresql://tally:tally-local-only@127.0.0.1"
SERVICES: dict[str, tuple[str, str, str]] = {
    # service: (migrations directory, database URL env var, local default URL)
    "ledger": (
        "services/ledger/migrations",
        "LEDGER_DATABASE_URL",
        f"{LOCAL}:55433/tally_ledger_v1",
    ),
    "gateway": ("services/api_gateway/migrations", "TALLY_DATABASE_URL", f"{LOCAL}:55432/tally"),
    "core": ("services/core/migrations", "TALLY_DATABASE_URL", f"{LOCAL}:55432/tally"),
    "vault": ("services/vault/migrations", "VAULT_DATABASE_URL", f"{LOCAL}:55434/tally_vault"),
    "recon": ("services/recon/migrations", "TALLY_DATABASE_URL", f"{LOCAL}:55432/tally"),
    "risk": ("services/risk/migrations", "TALLY_DATABASE_URL", f"{LOCAL}:55432/tally"),
    "backoffice": (
        "services/backoffice_api/migrations",
        "TALLY_DATABASE_URL",
        f"{LOCAL}:55432/tally",
    ),
}
_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


async def applied_versions(connection: asyncpg.Connection, table: str) -> set[int]:
    exists = await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
    if not exists:
        return set()
    return {int(row[0]) for row in await connection.fetch(f"SELECT version FROM {table}")}


async def migrate(service: str, url: str | None = None) -> list[str]:
    directory, env_var, default_url = SERVICES[service]
    url = url or os.environ.get(env_var, default_url)
    table = f"{service}_schema_migrations"
    files = sorted(p for p in Path(directory).glob("*.sql") if _FILE.match(p.name))
    applied: list[str] = []
    connection = await asyncpg.connect(url)
    try:
        # Serialise concurrent migrators for the same service.
        await connection.execute("SELECT pg_advisory_lock(hashtext($1))", table)
        done = await applied_versions(connection, table)
        for path in files:
            version = int(path.name[:4])
            if version in done:
                continue
            await connection.execute(path.read_text(encoding="utf-8"))
            if version not in await applied_versions(connection, table):
                raise RuntimeError(f"{path} did not record version {version} in {table}")
            applied.append(path.name)
    finally:
        await connection.close()
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="+", choices=sorted(SERVICES))
    args = parser.parse_args()
    for service in args.services:
        applied = asyncio.run(migrate(service))
        summary = ", ".join(applied) if applied else "already up to date"
        print(f"{service}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

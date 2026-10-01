"""Backup and restore drill for the ledger database (PostgreSQL base backup + WAL archive).

    uv run python -m scripts.backup_drill

Uses throwaway Docker containers and volumes (``tally-bk-*``) and never touches the Compose
stack. Steps:

1. Start a primary with WAL archiving (``archive_timeout`` 60 s), apply the ledger migrations and
   post entries continuously through ``ledger_post_entry`` (each acknowledged commit recorded).
2. Take a base backup while writes continue.
3. Mark a point in time, then post a "bad batch" (simulating a faulty release).
4. Disaster: kill the primary and delete its data volume. Un-archived WAL is lost with it.
5. Restore to the latest archived point; measure recovery time, recovered vs acknowledged
   entries (RPO), the integrity verifier, and that every recovered entry's hash equals the hash
   the primary had.
6. Point-in-time restore to the mark: the bad batch must be absent, everything before present.

AWS mapping: RDS automated backups (daily snapshot + transaction logs every 5 minutes, 35-day
PITR) and cross-region backup replication to ap-south-2 implement the same mechanism; this drill
measures it with PostgreSQL's own tools on local hardware.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from scripts.migrate import migrate

IMAGE = "postgres:16-alpine"
NET = "tally-bk-net"
PASSWORD = "drill-only"
VOLUMES = ("tally-bk-data", "tally-bk-archive", "tally-bk-base", "tally-bk-restore")
OUT = Path(".data/backup-drill")
POST = "SELECT ledger_post_entry($1, $2::jsonb)"


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {result.stderr.strip()}")
    return result.stdout.strip()


def cleanup() -> None:
    for name in ("tally-bk-primary", "tally-bk-restore"):
        docker("rm", "-f", name, check=False)
    for volume in VOLUMES:
        docker("volume", "rm", "-f", volume, check=False)
    docker("network", "rm", NET, check=False)


def start_postgres(name: str, data_volume: str, port: int, extra: list[str]) -> None:
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        NET,
        "-p",
        f"127.0.0.1:{port}:5432",
        "-e",
        f"POSTGRES_PASSWORD={PASSWORD}",
        "-e",
        "POSTGRES_USER=tally",
        "-e",
        "POSTGRES_DB=tally_ledger",
        "-v",
        f"{data_volume}:/var/lib/postgresql/data",
        "-v",
        "tally-bk-archive:/archive",
        "-v",
        "tally-bk-base:/base",
        IMAGE,
        *extra,
    )


async def wait_ready(url: str, timeout: float = 300) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            connection = await asyncpg.connect(url, timeout=2)
            try:
                if not await connection.fetchval("SELECT pg_is_in_recovery()"):
                    return time.monotonic() - started
            finally:
                await connection.close()
        except (TimeoutError, OSError, asyncpg.PostgresError):
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"{url} did not become writable")


@dataclass
class Writer:
    url: str
    acknowledged: list[tuple[str, float]] = field(default_factory=list)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    prefix: str = "drill"

    async def run(self) -> None:
        connection = await asyncpg.connect(self.url)
        n = len(self.acknowledged)
        try:
            while not self.stop.is_set():
                key = f"{self.prefix}-{n}"
                postings = json.dumps(
                    [
                        {"account_id": "bk-cash", "direction": "debit", "amount_minor": 100 + n},
                        {
                            "account_id": "bk-merchant",
                            "direction": "credit",
                            "amount_minor": 100 + n,
                        },
                    ]
                )
                try:
                    await connection.fetchval(
                        "SELECT ledger_post_entry($1, $2::jsonb)", key, postings
                    )
                except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
                    return  # the primary died; nothing after this was acknowledged
                self.acknowledged.append((key, time.time()))
                n += 1
                await asyncio.sleep(0.02)
        finally:
            if not connection.is_closed():
                await connection.close()


async def entry_hashes(url: str) -> dict[str, str]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT idempotency_key, encode(entry_hash, 'hex') h FROM ledger_journal_entries"
        )
        return {r["idempotency_key"]: r["h"] for r in rows}
    finally:
        await connection.close()


async def integrity(url: str) -> dict[str, bool]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch("SELECT check_name, ok FROM ledger_verify_integrity()")
        return {r["check_name"]: r["ok"] for r in rows}
    finally:
        await connection.close()


def restore(target_time: str | None) -> None:
    """Fresh data volume from the base backup, replaying archived WAL (to a target if given)."""
    docker("rm", "-f", "tally-bk-restore", check=False)
    docker("volume", "rm", "-f", "tally-bk-restore", check=False)
    docker("volume", "create", "tally-bk-restore")
    target = (
        f"recovery_target_time = '{target_time}'\nrecovery_target_action = 'promote'\n"
        if target_time
        else ""
    )
    script = (
        "set -e; cp -a /base/data/. /restore/; "
        "printf \"restore_command = 'cp /archive/%%f %%p'\\n"
        + target.replace("\n", "\\n")
        + '" >> /restore/postgresql.auto.conf; touch /restore/recovery.signal; '
        "chown -R 70:70 /restore; chmod 700 /restore"
    )
    docker(
        "run",
        "--rm",
        "-v",
        "tally-bk-base:/base",
        "-v",
        "tally-bk-archive:/archive",
        "-v",
        "tally-bk-restore:/restore",
        IMAGE,
        "sh",
        "-c",
        script,
    )


async def drill() -> dict[str, Any]:
    cleanup()
    docker("network", "create", NET)
    for volume in VOLUMES:
        docker("volume", "create", volume)
    docker(
        "run",
        "--rm",
        "-v",
        "tally-bk-archive:/archive",
        "-v",
        "tally-bk-base:/base",
        IMAGE,
        "sh",
        "-c",
        "chown 70:70 /archive /base",
    )
    primary_url = f"postgresql://tally:{PASSWORD}@127.0.0.1:55490/tally_ledger"
    restore_url = f"postgresql://tally:{PASSWORD}@127.0.0.1:55491/tally_ledger"
    archive = [
        "-c",
        "wal_level=replica",
        "-c",
        "archive_mode=on",
        "-c",
        "archive_timeout=60",
        "-c",
        "archive_command=test ! -f /archive/%f && cp %p /archive/%f",
    ]
    start_postgres("tally-bk-primary", "tally-bk-data", 55490, archive)
    await wait_ready(primary_url)
    await migrate("ledger", primary_url)
    connection = await asyncpg.connect(primary_url)
    await connection.execute(
        """INSERT INTO ledger_accounts(account_id, account_type, currency, allow_negative)
           VALUES ('bk-cash', 'asset', 'INR', true), ('bk-merchant', 'liability', 'INR', false)"""
    )
    await connection.close()

    writer = Writer(primary_url)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(60)
    backup_started = time.monotonic()
    docker(
        "exec",
        "-u",
        "postgres",
        "tally-bk-primary",
        "pg_basebackup",
        "-U",
        "tally",
        "-D",
        "/base/data",
        "-X",
        "stream",
        "-c",
        "fast",
    )
    backup_seconds = time.monotonic() - backup_started
    await asyncio.sleep(90)
    # Count first: everything acknowledged before the mark is read committed before it.
    acknowledged_before_mark = len(writer.acknowledged)
    connection = await asyncpg.connect(primary_url)
    mark = (await connection.fetchval("SELECT clock_timestamp()")).isoformat()
    await connection.close()
    await asyncio.sleep(1)
    writer.prefix = "bad"  # a faulty release starts posting wrong entries
    await asyncio.sleep(75)
    before_disaster = await entry_hashes(primary_url)
    # Disaster: the instance and its disk are gone; un-archived WAL goes with them.
    docker("rm", "-f", "tally-bk-primary")
    disaster_at = time.time()
    writer.stop.set()
    await task
    docker("volume", "rm", "-f", "tally-bk-data")
    acknowledged = dict(writer.acknowledged)
    last_ack_key, last_ack_time = writer.acknowledged[-1]

    # Restore to the latest archived WAL.
    restore_started = time.monotonic()
    restore(None)
    start_postgres("tally-bk-restore", "tally-bk-restore", 55491, archive)
    await wait_ready(restore_url)
    restore_seconds = time.monotonic() - restore_started
    recovered = await entry_hashes(restore_url)
    latest_integrity = await integrity(restore_url)
    lost = [k for k in acknowledged if k not in recovered]
    last_recovered_time = max(acknowledged[k] for k in recovered if k in acknowledged)
    mismatched = [k for k, h in recovered.items() if before_disaster.get(k, h) != h]
    phantom = [k for k in recovered if k not in acknowledged]

    # Point-in-time restore to the mark, before the bad batch.
    pitr_started = time.monotonic()
    docker("rm", "-f", "tally-bk-restore")
    restore(mark)
    start_postgres("tally-bk-restore", "tally-bk-restore", 55491, [])
    await wait_ready(restore_url)
    pitr_seconds = time.monotonic() - pitr_started
    pitr = await entry_hashes(restore_url)
    pitr_integrity = await integrity(restore_url)
    pre_mark = [k for k, _ in writer.acknowledged[:acknowledged_before_mark]]
    cleanup()

    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "entries_acknowledged": len(acknowledged),
        "base_backup_seconds": round(backup_seconds, 1),
        "latest_restore": {
            "recovery_seconds": round(restore_seconds, 1),
            "entries_recovered": len(recovered),
            "acknowledged_entries_lost": len(lost),
            "rpo_seconds": round(last_ack_time - last_recovered_time, 1),
            "hash_mismatches": len(mismatched),
            "unacknowledged_entries_present": len(phantom),
            "integrity": latest_integrity,
            "last_acknowledged": last_ack_key,
        },
        "pitr_to_mark": {
            "mark": mark,
            "recovery_seconds": round(pitr_seconds, 1),
            "bad_entries_present": sum(1 for k in pitr if k.startswith("bad-")),
            "pre_mark_entries_missing": sum(1 for k in pre_mark if k not in pitr),
            "integrity": pitr_integrity,
        },
        "disaster_at": datetime.fromtimestamp(disaster_at, UTC).isoformat(timespec="seconds"),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(drill())
    finally:
        cleanup()
    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    ok = (
        report["latest_restore"]["hash_mismatches"] == 0
        and all(report["latest_restore"]["integrity"].values())
        and report["pitr_to_mark"]["bad_entries_present"] == 0
        and report["pitr_to_mark"]["pre_mark_entries_missing"] == 0
        and all(report["pitr_to_mark"]["integrity"].values())
        and report["latest_restore"]["rpo_seconds"] <= 300
    )
    print("BACKUP DRILL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

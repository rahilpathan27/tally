"""Anchor the audit chain head in write-once object storage.

The database chain proves internal consistency; an anchor proves the chain was not rewritten
after the anchor time, because the anchored head must still appear at the same sequence. In AWS
the bucket uses S3 Object Lock (compliance mode); locally SeaweedFS stands in without WORM.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> str: ...

    async def get(self, key: str) -> bytes: ...


async def anchor(pool: asyncpg.Pool, store: ObjectStore) -> dict[str, Any] | None:
    head = await pool.fetchrow(
        "SELECT seq, entry_hash, occurred_at FROM audit_log ORDER BY seq DESC LIMIT 1"
    )
    if head is None:
        return None
    last = await pool.fetchval("SELECT max(seq) FROM audit_anchors")
    if last == head["seq"]:
        return None
    document = json.dumps(
        {
            "seq": head["seq"],
            "entry_hash": bytes(head["entry_hash"]).hex(),
            "occurred_at": head["occurred_at"].isoformat(),
            "anchored_at": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
    ).encode()
    key = f"audit-anchors/{head['seq']:012d}.json"
    digest = await store.put(key, document)
    await pool.execute(
        """INSERT INTO audit_anchors(anchor_id, seq, entry_hash, object_key, object_sha256)
           VALUES ($1, $2, $3, $4, $5)""",
        uuid.uuid4(),
        head["seq"],
        head["entry_hash"],
        key,
        digest,
    )
    return {"seq": head["seq"], "object_key": key}


async def verify_anchors(pool: asyncpg.Pool, store: ObjectStore) -> list[str]:
    """Problems found (empty list means every anchor still matches the chain)."""
    problems = []
    chain = await pool.fetchrow("SELECT * FROM audit_verify()")
    if chain is None or not chain["ok"]:
        problems.append(f"chain broken at {None if chain is None else chain['broken_at']}")
    for row in await pool.fetch("SELECT * FROM audit_anchors ORDER BY seq"):
        stored = await store.get(row["object_key"])
        if hashlib.sha256(stored).hexdigest() != row["object_sha256"]:
            problems.append(f"anchor {row['seq']} object changed")
            continue
        anchored = json.loads(stored)
        current = await pool.fetchval("SELECT entry_hash FROM audit_log WHERE seq = $1", row["seq"])
        if current is None or bytes(current).hex() != anchored["entry_hash"]:
            problems.append(f"entry {row['seq']} no longer matches its anchor")
    return problems

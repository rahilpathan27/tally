"""Model and rule-set registry with an evaluation gate and dual control.

Champion promotion and rule-set activation change live decisions, so both go through
``maker_checker_requests``: one person proposes, a different person approves. Challenger (shadow)
promotion does not affect outcomes and needs only the evaluation gate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from services.risk.rules import RuleSet, RuleSetError

GATE_TOLERANCE = {"pr_auc": 0.02, "recall_at_fpr_0.01": 0.02}


class RegistryError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def artifact_digests(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("model.onnx", "model.txt", "metadata.json")
    }


def evaluation_gate(candidate: dict[str, Any], champion: dict[str, Any] | None) -> list[str]:
    """Reasons the candidate may not replace the champion (empty list means it passes)."""
    failures = []
    if candidate.get("onnx_max_abs_diff", 1.0) > 1e-4:
        failures.append("ONNX export does not match the trained model")
    if champion is None:
        return failures
    for metric, tolerance in GATE_TOLERANCE.items():
        if float(candidate.get(metric, 0.0)) < float(champion.get(metric, 0.0)) - tolerance:
            failures.append(
                f"{metric} {candidate.get(metric):.4f} is below champion "
                f"{champion.get(metric):.4f} minus {tolerance}"
            )
    return failures


async def register_model(pool: asyncpg.Pool, directory: Path, actor: str) -> asyncpg.Record:
    metadata = json.loads((directory / "metadata.json").read_text())
    row = await pool.fetchrow(
        """INSERT INTO risk_model_versions(
               version, stage, artifact_path, artifact_sha256, metrics, registered_by
           ) VALUES ($1, 'registered', $2, $3::jsonb, $4::jsonb, $5)
           ON CONFLICT (version) DO NOTHING RETURNING *""",
        metadata["version"],
        str(directory),
        json.dumps(artifact_digests(directory)),
        json.dumps(metadata["metrics"]),
        actor,
    )
    if row is None:
        existing = await pool.fetchrow(
            "SELECT * FROM risk_model_versions WHERE version = $1", metadata["version"]
        )
        assert existing is not None
        if json.loads(existing["artifact_sha256"]) != artifact_digests(directory):
            raise RegistryError(409, "VERSION_EXISTS", "A different artifact has this version.")
        return existing
    return row


async def _metrics(connection: asyncpg.Connection, version: str) -> dict[str, Any] | None:
    raw = await connection.fetchval(
        "SELECT metrics FROM risk_model_versions WHERE version = $1", version
    )
    return None if raw is None else json.loads(raw) if isinstance(raw, str) else dict(raw)


async def promote_challenger(pool: asyncpg.Pool, version: str, actor: str) -> asyncpg.Record:
    async with pool.acquire() as connection, connection.transaction():
        candidate = await _metrics(connection, version)
        if candidate is None:
            raise RegistryError(404, "MODEL_NOT_FOUND", "Model version is not registered.")
        failures = evaluation_gate(candidate, None)
        if failures:
            raise RegistryError(422, "GATE_FAILED", "; ".join(failures))
        await connection.execute(
            "UPDATE risk_model_versions SET stage = 'registered' WHERE stage = 'challenger'"
        )
        row = await connection.fetchrow(
            """UPDATE risk_model_versions SET stage = 'challenger', promoted_by = $2,
                   promoted_at = clock_timestamp()
               WHERE version = $1 AND stage <> 'champion' RETURNING *""",
            version,
            actor,
        )
        if row is None:
            raise RegistryError(409, "ALREADY_CHAMPION", "Version is the champion.")
        return row


async def propose(
    pool: asyncpg.Pool, action_type: str, subject: str, payload: dict[str, Any], maker: str
) -> asyncpg.Record:
    try:
        row = await pool.fetchrow(
            """INSERT INTO maker_checker_requests(
                   request_id, action_type, subject_id, payload, maker, status
               ) VALUES ($1, $2, $3, $4::jsonb, $5, 'pending') RETURNING *""",
            uuid.uuid4(),
            action_type,
            subject,
            json.dumps(payload),
            maker,
        )
    except asyncpg.UniqueViolationError as exc:
        raise RegistryError(409, "PROPOSAL_PENDING", "A proposal is already pending.") from exc
    assert row is not None
    return row


async def propose_champion(pool: asyncpg.Pool, version: str, maker: str) -> asyncpg.Record:
    async with pool.acquire() as connection:
        candidate = await _metrics(connection, version)
        if candidate is None:
            raise RegistryError(404, "MODEL_NOT_FOUND", "Model version is not registered.")
        champion_version = await connection.fetchval(
            "SELECT version FROM risk_model_versions WHERE stage = 'champion'"
        )
        champion = (
            None if champion_version is None else await _metrics(connection, champion_version)
        )
    failures = evaluation_gate(candidate, champion)
    if failures:
        raise RegistryError(422, "GATE_FAILED", "; ".join(failures))
    return await propose(pool, "model_promotion", version, {"version": version}, maker)


async def propose_rules(
    pool: asyncpg.Pool, definition: dict[str, Any], maker: str
) -> asyncpg.Record:
    try:
        RuleSet(definition)
    except RuleSetError as exc:
        raise RegistryError(422, "INVALID_RULES", str(exc)) from exc
    return await propose(pool, "risk_rules", "active", {"definition": definition}, maker)


async def decide(
    pool: asyncpg.Pool, request_id: uuid.UUID, checker: str, approve: bool, reason: str
) -> asyncpg.Record:
    async with pool.acquire() as connection, connection.transaction():
        request = await connection.fetchrow(
            "SELECT * FROM maker_checker_requests WHERE request_id = $1 FOR UPDATE", request_id
        )
        if request is None or request["action_type"] not in {"model_promotion", "risk_rules"}:
            raise RegistryError(404, "REQUEST_NOT_FOUND", "Approval request was not found.")
        if request["status"] != "pending":
            raise RegistryError(409, "REQUEST_DECIDED", f"Request is {request['status']}.")
        if request["maker"] == checker:
            raise RegistryError(403, "MAKER_CANNOT_APPROVE", "The maker cannot approve.")
        payload = request["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        status = "rejected"
        result: dict[str, Any] = {}
        if approve:
            status = "executed"
            if request["action_type"] == "model_promotion":
                await connection.execute(
                    "UPDATE risk_model_versions SET stage = 'retired' WHERE stage = 'champion'"
                )
                await connection.execute(
                    """UPDATE risk_model_versions SET stage = 'champion', promoted_by = $2,
                           promoted_at = clock_timestamp() WHERE version = $1""",
                    payload["version"],
                    checker,
                )
                result = {"champion": payload["version"]}
            else:
                version = int(
                    await connection.fetchval(
                        "SELECT coalesce(max(version), 0) + 1 FROM risk_rule_versions"
                    )
                )
                await connection.execute(
                    "UPDATE risk_rule_versions SET active = false WHERE active"
                )
                await connection.execute(
                    """INSERT INTO risk_rule_versions(
                           version, definition, created_by, approved_by, active, activated_at
                       ) VALUES ($1, $2::jsonb, $3, $4, true, clock_timestamp())""",
                    version,
                    json.dumps({**payload["definition"], "version": version}),
                    request["maker"],
                    checker,
                )
                result = {"rule_version": version}
        row = await connection.fetchrow(
            """UPDATE maker_checker_requests SET status = $2, checker = $3,
                   decision_reason = $4, decided_at = clock_timestamp(), result = $5::jsonb
               WHERE request_id = $1 RETURNING *""",
            request_id,
            status,
            checker,
            reason,
            json.dumps(result),
        )
    assert row is not None
    return row


async def bootstrap(pool: asyncpg.Pool, model_dir: Path | None, rules: dict[str, Any]) -> None:
    """Seed rule version 1 and a champion when the registry is empty (local setup only)."""
    async with pool.acquire() as connection, connection.transaction():
        if not await connection.fetchval("SELECT 1 FROM risk_rule_versions LIMIT 1"):
            await connection.execute(
                """INSERT INTO risk_rule_versions(version, definition, created_by, active,
                                                  activated_at)
                   VALUES (1, $1::jsonb, 'bootstrap', true, clock_timestamp())""",
                json.dumps({**rules, "version": 1}),
            )
    if model_dir is not None and (model_dir / "metadata.json").exists():
        row = await register_model(pool, model_dir, "bootstrap")
        has_champion = await pool.fetchval(
            "SELECT 1 FROM risk_model_versions WHERE stage = 'champion'"
        )
        if not has_champion:
            await pool.execute(
                """UPDATE risk_model_versions SET stage = 'champion', promoted_by = 'bootstrap',
                       promoted_at = clock_timestamp() WHERE version = $1""",
                row["version"],
            )

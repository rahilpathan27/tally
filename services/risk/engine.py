"""Real-time decisioning: features -> rules -> model -> decision, logged with reason codes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import asyncpg
from libs.observability.metrics import RISK_DECISIONS, RISK_LATENCY
from pydantic import BaseModel, ConfigDict, Field

from services.risk.features import FeatureState, RiskEvent, compute_features
from services.risk.model import ModelRuntime
from services.risk.rules import DEFAULT_RULES, SEVERITY, RuleSet

REVIEW_SLA = timedelta(minutes=30)
CHALLENGE_TTL = timedelta(minutes=10)
ResolutionHook = Callable[[str, str], Awaitable[None]]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: Annotated[str, Field(min_length=1, max_length=64)]
    merchant_id: Annotated[str, Field(min_length=1, max_length=64)]
    amount_minor: Annotated[int, Field(gt=0, le=9_007_199_254_740_991)]
    currency: Literal["INR"] = "INR"
    method: Literal["card", "upi"]
    instrument_id: Annotated[str, Field(min_length=1, max_length=200)]
    payee_id: Annotated[str, Field(min_length=1, max_length=200)]
    device_id: Annotated[str, Field(min_length=1, max_length=200)] = "unknown-device"
    ip_address: Annotated[str, Field(min_length=1, max_length=64)] = "0.0.0.0"
    ip_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "IN"
    instrument_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "IN"
    account_age_days: Annotated[int, Field(ge=0, le=36_500)] = 365
    occurred_at: datetime | None = Field(default=None, strict=False)


class Reason(BaseModel):
    code: str
    description: str
    source: Literal["rule", "model"]


class DecisionResponse(BaseModel):
    decision_id: uuid.UUID
    payment_id: str
    decision: Literal["allow", "step_up", "review", "block"]
    model_version: str | None
    model_score: float | None
    rule_version: int | None
    reasons: list[Reason]
    review_case_id: uuid.UUID | None = None
    challenge_id: uuid.UUID | None = None
    latency_us: int
    replayed: bool = False


@dataclass(slots=True)
class RiskEngine:
    pool: asyncpg.Pool
    state: FeatureState
    step_up_secret: bytes
    model_root: Path = Path("ml/artifacts")
    on_resolution: ResolutionHook | None = None
    refresh_seconds: float = 5.0
    rules: RuleSet = field(default_factory=lambda: RuleSet(DEFAULT_RULES))
    champion: ModelRuntime | None = None
    challenger: ModelRuntime | None = None
    _loaded: dict[str, ModelRuntime] = field(default_factory=dict)
    _refreshed_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Configuration ---------------------------------------------------------------------------
    def _runtime(self, version: str, path: str) -> ModelRuntime:
        if version not in self._loaded:
            self._loaded[version] = ModelRuntime(Path(path), version)
        return self._loaded[version]

    async def refresh(self, *, force: bool = False) -> None:
        """Hot-reload the active rule set and champion/challenger models."""
        if not force and time.monotonic() - self._refreshed_at < self.refresh_seconds:
            return
        async with self._lock:
            rule_row = await self.pool.fetchrow(
                "SELECT version, definition FROM risk_rule_versions WHERE active"
            )
            if rule_row is not None and rule_row["version"] != self.rules.version:
                definition = rule_row["definition"]
                if isinstance(definition, str):
                    definition = json.loads(definition)
                self.rules = RuleSet({**definition, "version": rule_row["version"]})
            models = await self.pool.fetch(
                """SELECT version, stage, artifact_path FROM risk_model_versions
                   WHERE stage IN ('champion', 'challenger')"""
            )
            staged = {row["stage"]: row for row in models}
            self.champion = (
                self._runtime(staged["champion"]["version"], staged["champion"]["artifact_path"])
                if "champion" in staged
                else None
            )
            self.challenger = (
                self._runtime(
                    staged["challenger"]["version"], staged["challenger"]["artifact_path"]
                )
                if "challenger" in staged
                else None
            )
            self._refreshed_at = time.monotonic()

    # Decisions -------------------------------------------------------------------------------
    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        started = time.perf_counter_ns()
        existing = await self.pool.fetchrow(
            "SELECT * FROM risk_decisions WHERE payment_id = $1", request.payment_id
        )
        if existing is not None:
            return await self._replay(existing)
        await self.refresh()
        event = RiskEvent(
            payment_id=request.payment_id,
            merchant_id=request.merchant_id,
            occurred_at=request.occurred_at or datetime.now(UTC),
            amount_minor=request.amount_minor,
            method=request.method,
            instrument_id=request.instrument_id,
            payee_id=request.payee_id,
            device_id=request.device_id,
            ip_address=request.ip_address,
            ip_country=request.ip_country,
            instrument_country=request.instrument_country,
            account_age_days=request.account_age_days,
        )
        features = await compute_features(event, self.state)
        facts: dict[str, Any] = {
            **features,
            "amount_minor": request.amount_minor,
            "device_id": request.device_id,
            "instrument_id": request.instrument_id,
            "ip_address": request.ip_address,
            "payee_id": request.payee_id,
            "merchant_id": request.merchant_id,
        }
        hits = self.rules.evaluate(facts)
        reasons = [
            Reason(code=h.reason_code, description=h.description, source="rule") for h in hits
        ]
        model_decision = "allow"
        score = None
        if self.champion is not None:
            score = self.champion.score(features)
            model_decision = self.champion.decision(score.calibrated, request.method)
            reasons += [
                Reason(code=r.code, description=r.description, source="model")
                for r in score.reasons
            ]
        challenger = None
        if self.challenger is not None:  # shadow mode: logged, never affects the outcome
            challenger = self.challenger.score(features)
        actions = {h.action for h in hits}
        if "block" in actions:
            decision = "block"
        elif "allow" in actions:
            decision = "allow"
        else:
            decision = max([model_decision, *actions], key=lambda a: SEVERITY[a])
        RISK_DECISIONS.labels(decision).inc()
        # Every attempt, including blocked ones, feeds velocity features.
        await self.state.record(event)
        decision_id = uuid.uuid4()
        latency_us = (time.perf_counter_ns() - started) // 1_000
        RISK_LATENCY.observe(latency_us / 1_000_000)
        case_id: uuid.UUID | None = None
        challenge_id: uuid.UUID | None = None
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO risk_decisions(
                       decision_id, payment_id, merchant_id, decision, model_version,
                       model_score, model_decision, rule_version, rule_hits, reason_codes,
                       features, challenger_version, challenger_score, challenger_decision,
                       amount_minor, method, latency_us
                   ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb,
                             $12, $13, $14, $15, $16, $17)""",
                decision_id,
                request.payment_id,
                request.merchant_id,
                decision,
                None if score is None else score.version,
                None if score is None else score.calibrated,
                model_decision if score is not None else None,
                self.rules.version,
                json.dumps([h.rule_id for h in hits]),
                json.dumps([r.model_dump() for r in reasons]),
                json.dumps(features),
                None if challenger is None else challenger.version,
                None if challenger is None else challenger.calibrated,
                None
                if challenger is None or self.challenger is None
                else self.challenger.decision(challenger.calibrated, request.method),
                request.amount_minor,
                request.method,
                latency_us,
            )
            if decision == "review":
                case_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO risk_review_cases(
                           case_id, decision_id, payment_id, merchant_id, status, sla_due_at
                       ) VALUES ($1, $2, $3, $4, 'open', $5)""",
                    case_id,
                    decision_id,
                    request.payment_id,
                    request.merchant_id,
                    datetime.now(UTC) + REVIEW_SLA,
                )
            elif decision == "step_up":
                challenge_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO risk_step_up_challenges(
                           challenge_id, decision_id, payment_id, code_hash, status, expires_at
                       ) VALUES ($1, $2, $3, $4, 'pending', $5)""",
                    challenge_id,
                    decision_id,
                    request.payment_id,
                    hashlib.sha256(self.step_up_code(challenge_id).encode()).digest(),
                    datetime.now(UTC) + CHALLENGE_TTL,
                )
        return DecisionResponse(
            decision_id=decision_id,
            payment_id=request.payment_id,
            decision=decision,  # type: ignore[arg-type]
            model_version=None if score is None else score.version,
            model_score=None if score is None else round(score.calibrated, 6),
            rule_version=self.rules.version,
            reasons=reasons,
            review_case_id=case_id,
            challenge_id=challenge_id,
            latency_us=int(latency_us),
        )

    async def _replay(self, row: asyncpg.Record) -> DecisionResponse:
        reasons = row["reason_codes"]
        if isinstance(reasons, str):
            reasons = json.loads(reasons)
        case_id = await self.pool.fetchval(
            "SELECT case_id FROM risk_review_cases WHERE decision_id = $1", row["decision_id"]
        )
        challenge_id = await self.pool.fetchval(
            "SELECT challenge_id FROM risk_step_up_challenges WHERE decision_id = $1",
            row["decision_id"],
        )
        return DecisionResponse(
            decision_id=row["decision_id"],
            payment_id=row["payment_id"],
            decision=row["decision"],
            model_version=row["model_version"],
            model_score=row["model_score"],
            rule_version=row["rule_version"],
            reasons=[Reason(**r) for r in reasons],
            review_case_id=case_id,
            challenge_id=challenge_id,
            latency_us=row["latency_us"],
            replayed=True,
        )

    # Step-up (simulated OTP) -----------------------------------------------------------------
    def step_up_code(self, challenge_id: uuid.UUID) -> str:
        digest = hmac.new(self.step_up_secret, challenge_id.bytes, hashlib.sha256).digest()
        return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"

    async def verify_step_up(self, challenge_id: uuid.UUID, code: str) -> str:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM risk_step_up_challenges WHERE challenge_id = $1 FOR UPDATE",
                challenge_id,
            )
            if row is None:
                return "not_found"
            if row["status"] != "pending":
                return str(row["status"])
            if row["expires_at"] <= datetime.now(UTC):
                await connection.execute(
                    "UPDATE risk_step_up_challenges SET status = 'expired' WHERE challenge_id = $1",
                    challenge_id,
                )
                return "expired"
            ok = hmac.compare_digest(
                hashlib.sha256(code.encode()).digest(), bytes(row["code_hash"])
            )
            attempts = row["attempts"] + 1
            status = "verified" if ok else ("failed" if attempts >= 5 else "pending")
            await connection.execute(
                """UPDATE risk_step_up_challenges SET attempts = $2, status = $3
                   WHERE challenge_id = $1""",
                challenge_id,
                attempts,
                status,
            )
            return status if ok or status == "failed" else "incorrect"

    # Review queue ----------------------------------------------------------------------------
    async def resolve_review(
        self, case_id: uuid.UUID, actor: str, outcome: Literal["approve", "decline"], note: str
    ) -> asyncpg.Record:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM risk_review_cases WHERE case_id = $1 FOR UPDATE", case_id
            )
            if row is None:
                raise KeyError("case not found")
            if row["status"] != "open":
                raise ValueError(f"case is {row['status']}")
            status = "approved" if outcome == "approve" else "declined"
            updated = await connection.fetchrow(
                """UPDATE risk_review_cases SET status = $2, resolved_by = $3,
                       resolution_note = $4, resolved_at = clock_timestamp()
                   WHERE case_id = $1 RETURNING *""",
                case_id,
                status,
                actor,
                note,
            )
            await connection.execute(
                """INSERT INTO risk_review_actions(case_id, actor, action, note)
                   VALUES ($1, $2, $3, $4)""",
                case_id,
                actor,
                status,
                note,
            )
            # Analyst outcomes are training labels for the next model.
            payment_known = await connection.fetchval(
                "SELECT 1 FROM payment_intents WHERE payment_id::text = $1", row["payment_id"]
            )
            if payment_known:
                await connection.execute(
                    """INSERT INTO risk_labels(payment_id, merchant_id, label, source, source_id)
                       VALUES ($1::uuid, $2, $3, 'analyst_review', $4)
                       ON CONFLICT (source, source_id) DO NOTHING""",
                    row["payment_id"],
                    row["merchant_id"],
                    "legitimate" if outcome == "approve" else "fraud",
                    str(case_id),
                )
        if self.on_resolution is not None:
            await self.on_resolution(str(row["payment_id"]), outcome)
        assert updated is not None
        return updated

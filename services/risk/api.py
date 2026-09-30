"""Risk decision service: decisions, review queue, step-up, rules, models and drift."""

from __future__ import annotations

import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from services.risk import registry
from services.risk.drift import drift_report
from services.risk.engine import DecisionRequest, DecisionResponse, RiskEngine
from services.risk.features import FEATURE_NAMES, RedisFeatureState
from services.risk.rules import DEFAULT_RULES


class ResolveReview(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    outcome: Literal["approve", "decline"]
    note: Annotated[str, Field(min_length=1, max_length=2000)]


class VerifyStepUp(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    code: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class ProposeRules(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    definition: dict[str, Any]


class Decision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    reason: Annotated[str, Field(min_length=1, max_length=2000)]


def _engine(request: Request) -> RiskEngine:
    return cast(RiskEngine, request.app.state.risk_engine)


def actor(request: Request) -> str:
    expected = cast(str, getattr(request.app.state, "internal_key", ""))
    if not expected or not hmac.compare_digest(request.headers.get("x-internal-key", ""), expected):
        raise HTTPException(401, detail={"code": "UNAUTHENTICATED", "message": "Invalid key."})
    return request.headers.get("x-actor", "service") or "service"


Actor = Annotated[str, Depends(actor)]


def _row(row: asyncpg.Record | None) -> dict[str, Any]:
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Not found."})
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, uuid.UUID):
            out[key] = str(value)
        elif isinstance(value, datetime | date):
            out[key] = value.isoformat()
        elif isinstance(value, bytes):
            continue
        elif isinstance(value, str) and key in {
            "features",
            "rule_hits",
            "reason_codes",
            "payload",
            "result",
            "metrics",
            "definition",
            "artifact_sha256",
        }:
            out[key] = json.loads(value)
        else:
            out[key] = value
    return out


def create_app(engine: RiskEngine | None = None, internal_key: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "risk_engine", None) is not None:
            yield
            return
        pool = await asyncpg.create_pool(os.environ["TALLY_DATABASE_URL"], min_size=2, max_size=20)
        redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        core = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_CORE_URL", "http://127.0.0.1:8000"), timeout=10
        )
        key = os.environ.get("TALLY_INTERNAL_KEY", "")

        async def notify(payment_id: str, outcome: str) -> None:
            await core.post(
                f"/internal/v1/payments/{payment_id}/risk-resolution",
                json={"outcome": outcome},
                headers={"x-internal-key": os.environ.get("TALLY_RECOVERY_KEY", "")},
            )

        model_dir = Path(os.environ.get("TALLY_MODEL_DIR", "ml/artifacts/fraud-gbm-v1"))
        await registry.bootstrap(pool, model_dir, DEFAULT_RULES)
        app.state.risk_engine = RiskEngine(
            pool=pool,
            state=RedisFeatureState(redis),
            step_up_secret=os.environ.get("TALLY_STEP_UP_SECRET", "local-step-up").encode(),
            on_resolution=notify,
        )
        app.state.internal_key = key
        await app.state.risk_engine.refresh(force=True)
        try:
            yield
        finally:
            await core.aclose()
            await redis.aclose()
            await pool.close()

    app = FastAPI(title="Tally Risk", version="1.0.0", lifespan=lifespan)
    if engine is not None:
        app.state.risk_engine = engine
    app.state.internal_key = internal_key or ""

    @app.exception_handler(registry.RegistryError)
    async def registry_error(request: Request, exc: registry.RegistryError) -> JSONResponse:
        del request
        body = {"detail": {"code": exc.code, "message": exc.message}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/decisions", response_model=DecisionResponse)
    async def decide(body: DecisionRequest, request: Request, who: Actor) -> DecisionResponse:
        return await _engine(request).decide(body)

    @app.get("/v1/decisions")
    async def search_decisions(
        request: Request,
        who: Actor,
        decision: str | None = None,
        payment_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        rows = await _engine(request).pool.fetch(
            """SELECT * FROM risk_decisions
               WHERE ($1::text IS NULL OR decision = $1) AND ($2::text IS NULL OR payment_id = $2)
               ORDER BY created_at DESC LIMIT $3""",
            decision,
            payment_id,
            limit,
        )
        return [_row(row) for row in rows]

    @app.get("/v1/reviews")
    async def reviews(request: Request, who: Actor, status: str = "open") -> list[dict[str, Any]]:
        rows = await _engine(request).pool.fetch(
            """SELECT c.*, d.decision, d.model_score, d.reason_codes, d.amount_minor, d.method,
                      d.features, c.sla_due_at < clock_timestamp() AS sla_breached
               FROM risk_review_cases c JOIN risk_decisions d USING (decision_id)
               WHERE c.status = $1 ORDER BY c.sla_due_at LIMIT 200""",
            status,
        )
        return [_row(row) for row in rows]

    @app.post("/v1/reviews/{case_id}/resolve")
    async def resolve(
        case_id: uuid.UUID, body: ResolveReview, request: Request, who: Actor
    ) -> dict[str, Any]:
        try:
            row = await _engine(request).resolve_review(case_id, who, body.outcome, body.note)
        except KeyError as exc:
            raise HTTPException(
                404, detail={"code": "CASE_NOT_FOUND", "message": "No case."}
            ) from exc
        except ValueError as exc:
            raise HTTPException(409, detail={"code": "CASE_CLOSED", "message": str(exc)}) from exc
        return _row(row)

    @app.post("/v1/step_up/{challenge_id}/verify")
    async def verify(
        challenge_id: uuid.UUID, body: VerifyStepUp, request: Request, who: Actor
    ) -> dict[str, str]:
        return {"status": await _engine(request).verify_step_up(challenge_id, body.code)}

    @app.get("/internal/v1/step_up/{challenge_id}/code", include_in_schema=False)
    async def reveal(challenge_id: uuid.UUID, request: Request, who: Actor) -> dict[str, str]:
        """Payer-simulator only: stands in for the OTP delivered to the payer's phone."""
        return {"code": _engine(request).step_up_code(challenge_id)}

    @app.get("/v1/rules")
    async def active_rules(request: Request, who: Actor) -> dict[str, Any]:
        engine_ = _engine(request)
        await engine_.refresh(force=True)
        return {"version": engine_.rules.version, "rules": engine_.rules.rules}

    @app.post("/v1/rules/proposals", status_code=201)
    async def propose_rules(body: ProposeRules, request: Request, who: Actor) -> dict[str, Any]:
        return _row(await registry.propose_rules(_engine(request).pool, body.definition, who))

    @app.get("/v1/models")
    async def models(request: Request, who: Actor) -> list[dict[str, Any]]:
        rows = await _engine(request).pool.fetch(
            "SELECT * FROM risk_model_versions ORDER BY created_at DESC"
        )
        return [_row(row) for row in rows]

    @app.post("/v1/models/{version}/challenger")
    async def challenger(version: str, request: Request, who: Actor) -> dict[str, Any]:
        row = await registry.promote_challenger(_engine(request).pool, version, who)
        await _engine(request).refresh(force=True)
        return _row(row)

    @app.post("/v1/models/{version}/champion-proposals", status_code=201)
    async def propose_champion(version: str, request: Request, who: Actor) -> dict[str, Any]:
        return _row(await registry.propose_champion(_engine(request).pool, version, who))

    @app.post("/v1/approvals/{request_id}/approve")
    async def approve(
        request_id: uuid.UUID, body: Decision, request: Request, who: Actor
    ) -> dict[str, Any]:
        row = await registry.decide(_engine(request).pool, request_id, who, True, body.reason)
        await _engine(request).refresh(force=True)
        return _row(row)

    @app.post("/v1/approvals/{request_id}/reject")
    async def reject(
        request_id: uuid.UUID, body: Decision, request: Request, who: Actor
    ) -> dict[str, Any]:
        return _row(
            await registry.decide(_engine(request).pool, request_id, who, False, body.reason)
        )

    @app.post("/v1/drift/run")
    async def run_drift(
        request: Request, who: Actor, sample: Annotated[int, Query(ge=50, le=100_000)] = 5_000
    ) -> dict[str, Any]:
        engine_ = _engine(request)
        await engine_.refresh(force=True)
        if engine_.champion is None:
            raise HTTPException(409, detail={"code": "NO_CHAMPION", "message": "No model."})
        rows = await engine_.pool.fetch(
            """SELECT features, model_score FROM risk_decisions
               WHERE model_version = $1 ORDER BY created_at DESC LIMIT $2""",
            engine_.champion.version,
            sample,
        )
        columns: dict[str, list[float]] = {name: [] for name in FEATURE_NAMES}
        columns["__score__"] = []
        for row in rows:
            features = (
                json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
            )
            for name in FEATURE_NAMES:
                columns[name].append(float(features[name]))
            if row["model_score"] is not None:
                columns["__score__"].append(float(row["model_score"]))
        report = drift_report(engine_.champion.metadata["baselines"], columns)
        alerts = sum(1 for item in report if item["status"] == "alert")
        report_id = uuid.uuid4()
        await engine_.pool.execute(
            """INSERT INTO risk_drift_reports(
                   report_id, model_version, sample_size, features, alerts
               ) VALUES ($1, $2, $3, $4::jsonb, $5)""",
            report_id,
            engine_.champion.version,
            len(rows),
            json.dumps(report),
            alerts,
        )
        return {
            "report_id": str(report_id),
            "sample_size": len(rows),
            "alerts": alerts,
            "features": report,
        }

    @app.get("/v1/model-performance")
    async def performance(request: Request, who: Actor) -> dict[str, Any]:
        """Decision mix, latency and precision/recall on decisions that have received labels."""
        pool = _engine(request).pool
        mix = await pool.fetch(
            """SELECT decision, count(*) AS n FROM risk_decisions
               WHERE created_at > clock_timestamp() - interval '24 hours' GROUP BY decision"""
        )
        latency = await pool.fetchrow(
            """SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_us) AS p50,
                      percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_us) AS p99
               FROM risk_decisions WHERE created_at > clock_timestamp() - interval '24 hours'"""
        )
        labelled = await pool.fetchrow(
            """SELECT
                   count(*) FILTER (WHERE d.decision <> 'allow' AND l.label = 'fraud') AS tp,
                   count(*) FILTER (WHERE d.decision <> 'allow' AND l.label = 'legitimate') AS fp,
                   count(*) FILTER (WHERE d.decision = 'allow' AND l.label = 'fraud') AS fn
               FROM risk_decisions d JOIN risk_labels l ON l.payment_id::text = d.payment_id"""
        )
        assert labelled is not None and latency is not None
        tp, fp, fn = int(labelled["tp"]), int(labelled["fp"]), int(labelled["fn"])
        return {
            "decision_mix_24h": {row["decision"]: row["n"] for row in mix},
            "latency_us_p50": latency["p50"],
            "latency_us_p99": latency["p99"],
            "labelled_precision": None if tp + fp == 0 else tp / (tp + fp),
            "labelled_recall": None if tp + fn == 0 else tp / (tp + fn),
            "labelled_count": tp + fp + fn,
        }

    return app


app = create_app()

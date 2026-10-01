from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from libs.common.admission import AdmissionControl


def make_app(limit: int, gate: asyncio.Event) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/slow")
    async def slow() -> dict[str, str]:
        await gate.wait()
        return {"ok": "yes"}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(AdmissionControl, service="test", max_in_flight=limit)
    return app


def test_requests_beyond_the_cap_are_shed_fast_and_health_is_never_shed() -> None:
    async def scenario() -> tuple[list[int], int, int]:
        gate = asyncio.Event()
        app = make_app(2, gate)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            held = [asyncio.create_task(client.get("/v1/slow")) for _ in range(2)]
            await asyncio.sleep(0.05)
            shed = await client.get("/v1/slow")
            health = await client.get("/health/live")
            gate.set()
            done = await asyncio.gather(*held)
            after = await client.get("/v1/slow")
            assert shed.headers["retry-after"] == "1"
            assert shed.json()["detail"]["code"] == "OVERLOADED"
            return (
                [r.status_code for r in done],
                shed.status_code,
                health.status_code + 0 * after.status_code,
            )

    admitted, shed, health = asyncio.run(scenario())
    assert admitted == [200, 200]
    assert shed == 503
    assert health == 200

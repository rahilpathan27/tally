import asyncio
import logging

import httpx
from fastapi import FastAPI
from libs.observability.logging import RedactingJsonFormatter
from libs.observability.metrics import instrument
from libs.observability.tracing import CorrelationFilter, bind


def test_metrics_use_route_templates_not_raw_paths() -> None:
    app = FastAPI()
    instrument(app, "unit")

    @app.get("/v1/things/{thing_id}")
    async def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    async def run() -> str:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            for i in range(3):
                assert (await c.get(f"/v1/things/{i}")).status_code == 200
            return (await c.get("/metrics")).text

    text = asyncio.run(run())
    assert 'route="/v1/things/{thing_id}"' in text
    assert 'route="/v1/things/1"' not in text
    expected = (
        'tally_http_requests_total{method="GET",route="/v1/things/{thing_id}",'
        'service="unit",status="200"} 3.0'
    )
    assert expected in text


def test_log_records_carry_bound_business_ids() -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "captured", None, None)
    with bind(payment_id="pay_1", merchant_id="m_1"):
        CorrelationFilter().filter(record)
    line = RedactingJsonFormatter("unit").format(record)
    assert '"payment_id":"pay_1"' in line and '"merchant_id":"m_1"' in line

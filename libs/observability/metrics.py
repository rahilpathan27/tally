"""Prometheus metrics shared by every service.

RED metrics come from ``MetricsMiddleware`` and use the *route template* (``/v1/payments/{id}``)
as a label, never the raw path, so label cardinality stays bounded. Domain metrics are declared
here once so dashboards and alert rules have a single source of names.
"""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "tally_http_requests_total", "HTTP requests", ["service", "method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "tally_http_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "route"],
    buckets=LATENCY_BUCKETS,
)
HTTP_IN_FLIGHT = Gauge("tally_http_in_flight_requests", "Requests in progress", ["service"])

# Payments and switch
PAYMENT_TRANSITIONS = Counter(
    "tally_payment_transitions_total", "Accepted payment state transitions", ["method", "to_state"]
)
PAYMENTS_BY_STATE = Gauge("tally_payments_in_state", "Payments currently in a state", ["state"])
PENDING_UNKNOWN_OLDEST = Gauge(
    "tally_pending_unknown_oldest_age_seconds", "Age of the oldest pending_unknown payment"
)
RECOVERY_LAG = Gauge(
    "tally_recovery_lag_seconds", "How overdue the oldest scheduled status check is"
)
BANK_OUTCOMES = Counter(
    "tally_bank_outcomes_total", "Bank transfer outcomes seen by the switch", ["bank", "outcome"]
)
BREAKER_OPEN = Gauge("tally_circuit_breaker_open", "1 when a circuit breaker is open", ["breaker"])
IDEMPOTENT_REPLAYS = Counter(
    "tally_idempotency_replays_total", "Requests answered from a stored idempotent response"
)

# Ledger
LEDGER_POST_LATENCY = Histogram(
    "tally_ledger_post_duration_seconds",
    "Ledger posting latency",
    ["operation"],
    buckets=LATENCY_BUCKETS,
)
LEDGER_POSTS = Counter("tally_ledger_posts_total", "Ledger postings", ["operation", "result"])
LEDGER_INTEGRITY_OK = Gauge(
    "tally_ledger_integrity_ok", "1 when every ledger integrity check passes", ["check"]
)
LEDGER_INTEGRITY_RUNS = Counter("tally_ledger_integrity_runs_total", "Integrity verifier runs")

# Reconciliation
RECON_MATCH_RATE = Gauge("tally_recon_match_rate", "Matched bank lines / bank lines", ["source"])
RECON_OPEN_BREAKS = Gauge("tally_recon_open_breaks", "Open reconciliation breaks", ["type"])
RECON_OPEN_VALUE = Gauge(
    "tally_recon_open_break_value_minor", "Open break value in minor units", ["source"]
)

# Risk
RISK_DECISIONS = Counter("tally_risk_decisions_total", "Risk decisions", ["decision"])
RISK_LATENCY = Histogram(
    "tally_risk_decision_duration_seconds", "Risk decision latency", buckets=LATENCY_BUCKETS
)
RISK_FAIL_POLICY = Counter(
    "tally_risk_unavailable_total", "Payments decided by fail policy", ["mode"]
)
MODEL_DRIFT_PSI = Gauge("tally_model_drift_psi", "Latest PSI per feature", ["feature"])
REVIEW_QUEUE = Gauge("tally_risk_review_queue", "Open review cases")

# Money movement and webhooks
WEBHOOK_BACKLOG = Gauge("tally_webhook_backlog", "Webhook deliveries waiting to be sent")
WEBHOOK_OLDEST = Gauge(
    "tally_webhook_oldest_pending_seconds", "Age of the oldest undelivered webhook"
)
WEBHOOK_RESULTS = Counter("tally_webhook_deliveries_total", "Webhook attempts", ["result"])
OUTBOX_BACKLOG = Gauge("tally_outbox_unpublished", "Outbox events not yet published")
PAYOUTS_RETURNED = Counter("tally_payouts_returned_total", "Payouts returned by the bank")
SETTLEMENT_FAILURES = Counter("tally_settlement_failures_total", "Settlement runs that failed")

# Security events
AUTH_EVENTS = Counter("tally_auth_events_total", "Authentication events", ["event"])
RATE_LIMITED = Counter("tally_rate_limited_total", "Requests rejected by rate limits")
LOAD_SHED = Counter(
    "tally_load_shed_total", "Requests refused by admission control (overload)", ["service"]
)
SSRF_BLOCKED = Counter("tally_ssrf_blocked_total", "Webhook deliveries blocked by SSRF checks")

# Per-merchant usage (merchant IDs are a bounded, known set in this simulation)
MERCHANT_REQUESTS = Counter(
    "tally_merchant_api_requests_total", "Merchant API requests", ["merchant", "status_class"]
)


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware:
    def __init__(self, app: ASGIApp, *, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        HTTP_IN_FLIGHT.labels(self.service).inc()

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            HTTP_IN_FLIGHT.labels(self.service).dec()
            route = _route_template(scope)
            method = str(scope.get("method", "GET"))
            HTTP_REQUESTS.labels(self.service, method, route, str(status)).inc()
            HTTP_LATENCY.labels(self.service, method, route).observe(time.perf_counter() - started)


async def metrics_endpoint(request: Request) -> Response:
    del request
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def instrument(app: Any, service: str) -> None:
    """Add RED metrics and ``GET /metrics`` to a FastAPI app."""
    app.add_middleware(MetricsMiddleware, service=service)
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)

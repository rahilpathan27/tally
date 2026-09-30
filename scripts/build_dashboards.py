"""Generate Grafana dashboards (JSON) from compact panel definitions.

Run ``uv run python -m scripts.build_dashboards``; output goes to
``infra/observability/grafana/dashboards``. Keeping panels in code makes them reviewable and
keeps metric names in sync with ``libs/observability/metrics.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path("infra/observability/grafana/dashboards")
Panel = tuple[str, str, str, str]  # title, kind (stat|timeseries|table), unit, PromQL

DASHBOARDS: dict[str, tuple[str, list[Panel]]] = {
    "payments-overview": (
        "Payments Overview",
        [
            (
                "Transitions / s by state",
                "timeseries",
                "ops",
                "sum by (to_state) (rate(tally_payment_transitions_total[5m]))",
            ),
            ("Payments in state", "timeseries", "short", "tally_payments_in_state"),
            (
                "Success ratio (5m)",
                "stat",
                "percentunit",
                'sum(rate(tally_payment_transitions_total{to_state="succeeded"}[5m])) / '
                'sum(rate(tally_payment_transitions_total{to_state=~"succeeded|failed|reversed"}[5m]))',
            ),
            (
                "Merchant API p99 latency",
                "timeseries",
                "s",
                'histogram_quantile(0.99, sum by (le, route) (rate(tally_http_request_duration_seconds_bucket{service="core",route=~"/v1/.*"}[5m])))',
            ),
            (
                "Merchant API 5xx ratio",
                "timeseries",
                "percentunit",
                'sum(rate(tally_http_requests_total{service="core",route=~"/v1/.*",status=~"5.."}[5m])) / '
                'sum(rate(tally_http_requests_total{service="core",route=~"/v1/.*"}[5m]))',
            ),
            (
                "Idempotent replays / s",
                "timeseries",
                "ops",
                "rate(tally_idempotency_replays_total[5m])",
            ),
        ],
    ),
    "switch-bank-health": (
        "Switch and Bank Health",
        [
            (
                "Bank success rate",
                "timeseries",
                "percentunit",
                'sum by (bank) (rate(tally_bank_outcomes_total{outcome="approved"}[5m])) / '
                "sum by (bank) (rate(tally_bank_outcomes_total[5m]))",
            ),
            (
                "Bank outcomes / s",
                "timeseries",
                "ops",
                "sum by (bank, outcome) (rate(tally_bank_outcomes_total[5m]))",
            ),
            ("Oldest pending_unknown", "stat", "s", "tally_pending_unknown_oldest_age_seconds"),
            ("Recovery lag", "stat", "s", "tally_recovery_lag_seconds"),
            ("Circuit breakers (1 = open)", "timeseries", "short", "tally_circuit_breaker_open"),
        ],
    ),
    "ledger-health": (
        "Ledger Health",
        [
            ("Integrity checks (1 = ok)", "timeseries", "short", "tally_ledger_integrity_ok"),
            (
                "Postings / s by result",
                "timeseries",
                "ops",
                "sum by (operation, result) (rate(tally_ledger_posts_total[5m]))",
            ),
            (
                "Posting p99 latency",
                "timeseries",
                "s",
                "histogram_quantile(0.99, sum by (le, operation) (rate(tally_ledger_post_duration_seconds_bucket[5m])))",
            ),
            (
                "Verifier runs (15m)",
                "stat",
                "short",
                "increase(tally_ledger_integrity_runs_total[15m])",
            ),
        ],
    ),
    "reconciliation": (
        "Reconciliation",
        [
            ("Match rate", "stat", "percentunit", "tally_recon_match_rate"),
            ("Open breaks by type", "timeseries", "short", "tally_recon_open_breaks"),
            (
                "Open break value (₹)",
                "stat",
                "currencyINR",
                "tally_recon_open_break_value_minor / 100",
            ),
        ],
    ),
    "risk-model-health": (
        "Risk and Model Health",
        [
            (
                "Decisions / s",
                "timeseries",
                "ops",
                "sum by (decision) (rate(tally_risk_decisions_total[5m]))",
            ),
            (
                "Decision p99 latency",
                "timeseries",
                "s",
                "histogram_quantile(0.99, sum by (le) (rate(tally_risk_decision_duration_seconds_bucket[5m])))",
            ),
            (
                "Fail-policy decisions (15m)",
                "stat",
                "short",
                "sum(increase(tally_risk_unavailable_total[15m]))",
            ),
            ("Feature drift (PSI)", "table", "short", "tally_model_drift_psi"),
            ("Review queue", "stat", "short", "tally_risk_review_queue"),
        ],
    ),
    "security-events": (
        "Security Events",
        [
            (
                "Auth events / 10m",
                "timeseries",
                "short",
                "sum by (event) (increase(tally_auth_events_total[10m]))",
            ),
            (
                "Rate-limited requests / s",
                "timeseries",
                "ops",
                "rate(tally_rate_limited_total[5m])",
            ),
            ("SSRF blocks (1h)", "stat", "short", "increase(tally_ssrf_blocked_total[1h])"),
            (
                "Webhook results / s",
                "timeseries",
                "ops",
                "sum by (result) (rate(tally_webhook_deliveries_total[5m]))",
            ),
            ("Webhook backlog", "stat", "short", "tally_webhook_backlog"),
            ("Outbox backlog", "stat", "short", "tally_outbox_unpublished"),
        ],
    ),
    "per-merchant-usage": (
        "Per-Merchant Usage",
        [
            (
                "Requests / s by merchant",
                "timeseries",
                "ops",
                "sum by (merchant) (rate(tally_merchant_api_requests_total[5m]))",
            ),
            (
                "Error ratio by merchant",
                "timeseries",
                "percentunit",
                'sum by (merchant) (rate(tally_merchant_api_requests_total{status_class=~"4xx|5xx"}[5m])) / '
                "sum by (merchant) (rate(tally_merchant_api_requests_total[5m]))",
            ),
        ],
    ),
}


def panel(index: int, spec: Panel) -> dict[str, Any]:
    title, kind, unit, expr = spec
    width = 12 if kind != "stat" else 6
    return {
        "id": index + 1,
        "title": title,
        "type": kind,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": {"h": 8, "w": width, "x": (index % 2) * 12, "y": (index // 2) * 8},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"refId": "A", "expr": expr, "datasource": {"uid": "prometheus"}}],
        "options": {},
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for uid, (title, panels) in DASHBOARDS.items():
        dashboard = {
            "uid": f"tally-{uid}",
            "title": title,
            "tags": ["tally"],
            "timezone": "Asia/Kolkata",
            "refresh": "30s",
            "schemaVersion": 39,
            "time": {"from": "now-1h", "to": "now"},
            "panels": [panel(i, spec) for i, spec in enumerate(panels)],
        }
        (OUT / f"{uid}.json").write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(DASHBOARDS)} dashboards to {OUT}")


if __name__ == "__main__":
    main()
